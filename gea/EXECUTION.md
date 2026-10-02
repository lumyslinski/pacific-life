# How a run executes

Snowflake computes. PostgreSQL decides when a run may start and keeps the state the user sees.
Python carries messages between the two and never computes, waits for, or sequences a step.

```text
User                      GEA API (Python)            PostgreSQL                  Relay (Python)              Snowflake
----                      ----------------            ----------                  --------------              ---------
Submit a run       ---->  POST /runs/{id}/submit ---> contract + pending
or a job of runs          POST /jobs                  delivery, run "queued"
                                                      claim: next contract <----  ClaimContractDeliveries
                                                      allowed to start            MERGE contract        ----> DATA_CONTRACT
                                                                                  EXECUTE TASK          ----> RUN_PIPELINE (one graph run)
                                                                                                              step 1 .. step 7, each:
                                                                                                              RUN_STEP -> GEA.STEP.<step>
                                                      status, steps, log   <----  copy what changed    <----  RUN_STATUS, RUN_STEP_STATUS, RUN_LOG
Watch              ---->  GET /runs/{id}/execution <-- RunExecution(Step)
                          GET /runs/{id}/logs      <-- RunLog
                          GET /jobs/{id}           <-- JobSummary
Cancel             ---->  POST /runs/{id}/cancel ---> cancel request       ---->  pass on              ---->  RUN_STATUS.CANCEL_REQUESTED_AT
Resolve a failure  ---->  POST /runs/{id}/resolution
```

## Who does what

| Part | Where it runs | What it does | State |
|---|---|---|---|
| Step logic: data and set up, segmentation, actuals, IBNR, assigning expected, ultimate calculation, actual / expected | Snowflake, one stored procedure per step (`GEA.STEP.*`) | Set-based SQL next to the data. Reads its configuration from the contract and the output of earlier steps by contract id; writes its own output by contract id, replacing it on a re-run | draft SQL, placeholders |
| A calculation SQL cannot express (fitting, an enrichment model) | Snowpark Python procedure, still in Snowflake | The same signature and the same place in the pipeline as any other step; the data does not leave Snowflake | not needed yet |
| Order of the steps | Snowflake task graph `GEA.CONTROL.RUN_PIPELINE` | One graph run per contract. Records status before and after every step; a failed step stops the steps after it; a finalizer writes the terminal status | draft SQL |
| When a run starts | PostgreSQL, `gea."ClaimContractDeliveries"` | Hands contracts to the relay: a run on its own at once, the runs of a job in job order and `MaxParallel` at a time | built, tested |
| Carrying contracts over, copying status back, passing a cancel on | Relay: a small Python worker with the role `gea_relay` | No computation and no waiting: every call is short and can be repeated | **not built** |
| What the user sees and decides | GEA API | Reads `RunExecution`, `RunLog`, `JobSummary`; records cancel and resolution | built, tested |

A separate Python worker that computes is only justified for a step that Snowflake cannot host at all
(a library that is not available there, a call to an outside service). It would then be one more engine
behind the same step contract, with its state in the same status tables. None is planned.

## Why not Python

A calculation that runs for minutes or hours inside an API process has its state in memory: nobody
can see where it is, a deployment or a crash loses it, and two of them compete for one machine.
The existing calculation module shows the limit: it serialises every writer with one process lock.
Moving the data out of Snowflake to compute on it in Python adds a copy and a transfer for work
that is set-based by nature (segmentation, chain ladder, actual to expected).

## State: where it is and how it is observed

State is rows, written before and after every step, in the system that does the work, and copied to
the system the user talks to.

| Question | Answered by | Source |
|---|---|---|
| Has the run started? If not, why? | `GET /runs/{id}/execution` -> `status`, `delivery` (pending: waiting for its turn in a job or for a retry; delivering; delivered; failed; cancelled) | `ContractDelivery` |
| Which step is running, which are done? | `execution.steps[]`: `pending`, `running`, `complete`, `failed`, `skipped`, `reused`, `pending-rerun`, each with start, end and what the step reported (row counts, error) | `RUN_STEP_STATUS` -> `RunExecutionStep` |
| Is it still alive? | `execution.lastReportedAt`. The relay reports a contract while Snowflake shows its pipeline as executing; an execution Snowflake no longer knows about is failed with a reason instead of staying `running` | `RunExecution.UpdatedAt`, `gea."FailStaleExecutions"` |
| What happened? | `GET /runs/{id}/logs` | `RUN_LOG` -> `RunLog` |
| How far is the batch? | `GET /jobs/{id}`: `status`, counters, `progressPercentage`, `durationSeconds`; `GET /runs?jobId=` for the runs | view `JobSummary` |
| What did it cost, which statement was slow? | Snowflake's own `QUERY_HISTORY` and task history: every statement of a step is tagged `gea:<contractId>:<stepKey>` | Snowflake |

The Vue client polls with `If-None-Match`: the answer is a 304 without a body until something moved
(`useRunWizard.pollUntilFinished`, `useJobMonitor.pollUntilFinished`). A push channel can replace the
polling later without changing where the state lives.

## Sequence and parallel

**Inside a run**: the steps run in the order of the contract. They are a chain today because each
step of the workbook reads what the one before it wrote. The order is a task graph, so two steps that
do not depend on each other become parallel by giving them the same predecessor; nothing else changes.

**Between runs**: every run is its own graph run in Snowflake, isolated by its contract id, so runs
execute side by side. How many, and in which order, is decided when they are submitted:

| Submitted as | Starts | Limit |
|---|---|---|
| One run, `POST /runs/{id}/submit` | when the relay next claims deliveries | the warehouse |
| A job with `maxParallel` omitted | all its runs at once | the warehouse |
| A job with `maxParallel: n` | in the order of `runIds`, at most `n` executing at a time | `n` |
| A job with `maxParallel: 1` | strictly one after another | 1 |

Rules of a job, all enforced and tested in PostgreSQL (`tests/smoke.sql` group 13):

- A run may start while fewer than `MaxParallel` other runs of the job are ahead of it or already started.
- A run that fails does not stop the job: the next one starts. The job ends `completed-with-failures`.
- A run whose delivery cannot be sent yet (retry with backoff) keeps its place: later runs wait.
  One whose delivery gave up no longer holds the others back; it shows in the job as still queued
  until an operator retries or cancels it.
- A failed run that is fixed and submitted again stays in its job and counts towards its limit.

## Cancel and failure

| | What happens |
|---|---|
| Cancel before the contract is sent | The delivery is withdrawn; the run is `failed` at once with "Cancelled by ... before execution started." |
| Cancel while it executes | `cancelRequestedAt` is recorded; the relay writes it to Snowflake; the pipeline stops before its next step and reports `failed` with "Cancelled by ...". A long step can also be stopped at once by cancelling its tagged statements |
| A step fails | The step is `failed` with the error, the later steps are `skipped`, the run is `failed` with "Step ... failed: ..." |
| The execution goes silent | `failed` with "No status from Snowflake since ...; the execution is presumed lost." |
| After any failure | `POST /runs/{id}/resolution`: `rerun-with-fixed-config` returns the run to draft (the next submit is contract version n+1), `mark-resolved` accepts the failure with a note |

The workbook has no run status "cancelled", so a cancelled run is a failed run with the reason, and it
is resolved like any other failure.

Submitting, cancelling and resolving need a role that may prepare (the workbook's "the current user is not a
Viewer" on those buttons); watching needs none. See [api/README.md](api/README.md#roles-and-permissions).

## What is built and what is not

| | State |
|---|---|
| PostgreSQL: jobs, delivery in job order, execution status and log, cancel, resolution, lost executions | built; `tests/smoke.sql` groups 13 to 16 |
| API and Vue client: submit, jobs, execution, logs, cancel, resolution | built; `tests/gea/test_api.py`, client checked end to end |
| Snowflake: status tables, pipeline, step registry, placeholders for the seven steps | **draft SQL, never executed**: [`db/snowflake`](db/snowflake); drawn on page 17 of [`../architecture`](../architecture/README.md) |
| Relay | **not built**. Its four duties and the order of its calls are written at the end of `db/snowflake/V002__run_pipeline.sql` |
| The calculations themselves | the modelling team's; the placeholders return without doing anything |

The local Snowflake emulator in this repository runs scalar SQL functions only; it cannot run a stored
procedure or a task. The Snowflake side therefore needs a real account to be tried, and five points to
be confirmed there first; they are listed at the end of `V002__run_pipeline.sql`. The one that decides
the design is the first: several graph runs of one task graph at the same time, each with its own
contract id (`EXECUTE TASK ... USING CONFIG` with `OVERLAP_POLICY = ALLOW_ALL_OVERLAP`). If that does
not hold up, the fallback is the same pipeline as one stored procedure that the relay calls per
contract; the status tables, the API and PostgreSQL stay as they are.

## Open points

1. Step dependencies: are all seven steps a chain, or can some run side by side (for example actuals
   and assigning expected)? The modelling team decides; the graph is the only thing that changes.
2. Reuse of steps (the workbook's status `reused`): needs an input data version in the contract
   (`Run.DataVersion` of sheet 03, not a field of "POC Data") and results addressable by a
   fingerprint of step configuration and inputs.
3. Should a failed run stop the rest of a sequential job? Today it does not.
4. Warehouse sizing and a limit on concurrent runs outside jobs.
5. How long an execution may be silent before it is failed (default in `FailStaleExecutions`: 30 minutes).
