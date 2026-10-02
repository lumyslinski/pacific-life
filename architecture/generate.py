"""Generate the architecture diagrams as matching SVG and editable draw.io files.

Run from any directory: python architecture/generate.py
Only Python's standard library is required. Every page is defined once as a
scene; the SVG is the vector preview and the draw.io file uses uncompressed
mxGraph XML with explicit white labels and dark text. Pages 1-3 show the
revision 3 target layers; pages 4-7 show the implemented version 2 runtime;
page 8 shows proposed contract storage and AWS run orchestration.
Page 9 is the main actor sequence: Core transforms Bronze to Silver, then
a user-selected custom process applies input overrides and produces Gold.
Page 10 explains worker responsibilities. Pages 11-12 are actor/lifeline
sequences for Data Contract resolution, asynchronous execution and retry.
Pages 13-17 cover the GEA module: components, PostgreSQL data model, how the
wizard saves the run, submit with delivery to Snowflake, and run execution.
This script changes documentation only, not application behavior.
"""
from pathlib import Path
from xml.etree import ElementTree as ET
import html
import textwrap

OUT = Path(__file__).resolve().parent
DRAWIO = "calculation-architecture.drawio"
INK = "#172B46"
BLUE = "#356CC9"
GREEN = "#24795C"
AMBER = "#A96915"
GRAY = "#738096"
BRONZE = "#95612E"
FONT = "Arial"


class Page:
    def __init__(self, name, title, subtitle, width=1440, height=1160):
        self.name, self.width, self.height, self.shapes = name, width, height, []
        self.text(40, 24, width - 80, 46, title, 29, True, "left")
        self.text(40, 78, width - 80, 34, subtitle, 17, False, "left", GRAY)

    def box(self, x, y, w, h, label="", color=BLUE, fill="#FFFFFF", dashed=False, size=18):
        self.shapes.append(dict(kind="box", x=x, y=y, w=w, h=h, color=color, fill=fill, dashed=dashed))
        if label:
            self.text(x + 12, y + 10, w - 24, h - 20, label, size)

    def text(self, x, y, w, h, label, size=17, bold=False, align="center", color=INK):
        self.shapes.append(dict(kind="text", x=x, y=y, w=w, h=h, label=label,
                                size=size, bold=bold, align=align, color=color))

    def line(self, points, label=None, label_at=None, dashed=False, arrow=True, color=BLUE):
        self.shapes.append(dict(kind="line", points=points, dashed=dashed, arrow=arrow, color=color))
        if label:
            x, y, w = label_at
            self.text(x, y, w, 32, label, 15, False, "center", color)

    def frame(self, x, y, w, h, title, color=GRAY, dashed=False):
        self.box(x, y, w, h, color=color, fill="none", dashed=dashed)
        self.text(x + 12, y - 17, min(w - 24, len(title) * 11 + 28), 34, title, 16, True, "left", color)

    def participants(self, entries, bottom):
        for x, label in entries:
            self.box(x - 123, 140, 246, 80, label, size=17)
            self.line([(x, 220), (x, bottom)], dashed=True, arrow=False, color=GRAY)

    def activation(self, x, y, h):
        self.box(x - 6, y, 12, h, color=BLUE, fill="#E6EEFC")

    def message(self, a, b, y, label, reply=False):
        self.line([(a, y), (b, y)], dashed=reply, color=GREEN if reply else BLUE)
        width = abs(a - b) - 14
        wrapped = textwrap.wrap(label, max(18, int(width / 7.1)), break_long_words=False, break_on_hyphens=False)
        self.text(min(a, b) + 7, y - 23 * len(wrapped) - 3, width, 23 * len(wrapped), "\n".join(wrapped), 14)

    def svg(self):
        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" viewBox="0 0 {self.width} {self.height}" role="img">',
                 f'<title>{html.escape(self.name)}</title>',
                 '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker></defs>',
                 f'<rect width="{self.width}" height="{self.height}" fill="#FFFFFF"/>']
        for s in self.shapes:
            if s["kind"] == "box":
                dash = ' stroke-dasharray="7 5"' if s["dashed"] else ""
                parts.append(f'<rect x="{s["x"]}" y="{s["y"]}" width="{s["w"]}" height="{s["h"]}" rx="3" fill="{s["fill"]}" stroke="{s["color"]}" stroke-width="1.7"{dash}/>')
            elif s["kind"] == "line":
                points = " ".join(f"{x},{y}" for x, y in s["points"])
                dash = ' stroke-dasharray="7 5"' if s["dashed"] else ""
                arrow = ' marker-end="url(#arrow)"' if s["arrow"] else ""
                parts.append(f'<polyline points="{points}" fill="none" stroke="{s["color"]}" stroke-width="1.7"{dash}{arrow}/>')
            else:
                lines = s["label"].split("\n")
                size = s["size"]
                leading = size * 1.42
                x = s["x"] + s["w"] / 2 if s["align"] == "center" else s["x"]
                first = s["y"] + s["h"] / 2 - (len(lines) - 1) * leading / 2 + size * .34
                anchor = "middle" if s["align"] == "center" else "start"
                # Explicit white label backgrounds survive light/dark viewers.
                parts.append(f'<rect x="{s["x"]}" y="{s["y"]}" width="{s["w"]}" height="{s["h"]}" fill="#FFFFFF"/>')
                for i, line in enumerate(lines):
                    weight = "700" if s["bold"] else "400"
                    parts.append(f'<text x="{x}" y="{first + i * leading}" text-anchor="{anchor}" font-family="{FONT}, sans-serif" font-size="{size}" font-weight="{weight}" fill="{s["color"]}">{html.escape(line)}</text>')
        parts.append("</svg>")
        return "\n".join(parts) + "\n"

    def drawio(self, parent, index):
        diagram = ET.SubElement(parent, "diagram", id=f"page-{index}", name=self.name)
        model = ET.SubElement(diagram, "mxGraphModel", dx=str(self.width), dy=str(self.height), grid="1", gridSize="10", guides="1", tooltips="1", connect="1", arrows="1", fold="1", page="1", pageScale="1", pageWidth=str(self.width), pageHeight=str(self.height), background="#FFFFFF", math="0", shadow="0")
        root = ET.SubElement(model, "root")
        ET.SubElement(root, "mxCell", id="0")
        ET.SubElement(root, "mxCell", id="1", parent="0")
        for i, s in enumerate(self.shapes, 2):
            base = f"html=0;fontFamily={FONT};fontColor={INK};labelBackgroundColor=#FFFFFF;labelBorderColor=none;"
            if s["kind"] == "line":
                style = base + f'rounded=0;endArrow={"block" if s["arrow"] else "none"};endFill=1;strokeWidth=1.7;strokeColor={s["color"]};dashed={int(s["dashed"])};'
                cell = ET.SubElement(root, "mxCell", id=str(i), style=style, edge="1", parent="1")
                geo = ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})
                for which, point in [("sourcePoint", s["points"][0]), ("targetPoint", s["points"][-1])]:
                    ET.SubElement(geo, "mxPoint", x=str(point[0]), y=str(point[1]), **{"as": which})
                if len(s["points"]) > 2:
                    arr = ET.SubElement(geo, "Array", **{"as": "points"})
                    for x, y in s["points"][1:-1]:
                        ET.SubElement(arr, "mxPoint", x=str(x), y=str(y))
            else:
                if s["kind"] == "box":
                    style = base + f'rounded=0;fillColor={s["fill"]};strokeColor={s["color"]};strokeWidth=1.7;dashed={int(s["dashed"])};'
                    value = ""
                else:
                    style = base + f'text;whiteSpace=wrap;overflow=hidden;fillColor=#FFFFFF;strokeColor=none;align={s["align"]};verticalAlign=middle;fontSize={s["size"]};fontStyle={int(s["bold"])};fontColor={s["color"]};'
                    value = s["label"]
                cell = ET.SubElement(root, "mxCell", id=str(i), value=value, style=style, vertex="1", parent="1")
                ET.SubElement(cell, "mxGeometry", x=str(s["x"]), y=str(s["y"]), width=str(s["w"]), height=str(s["h"]), **{"as": "geometry"})


# --- Target design: revision 3 data layers ---------------------------------

def layers():
    p = Page("01 Layer overview", "Calculation modeling · layer overview",
             "Revision 3 target · Python resolves Snowflake contracts; durable AWS run dispatch is detailed on page 8.", 1600, 1130)
    p.box(60, 150, 320, 150, "Vue workspace\nSubmit input / edit scenario\nRun / retry / inspect results", size=18)
    p.box(500, 150, 480, 150, "Litestar command API\nFreeze input, contract and scenario revisions\nCommit run + dispatch outbox; return 202\nVue polls GET /runs/{runId}", size=18)
    p.box(1110, 130, 430, 180, "Data Contract module in Python\nParameter schema + validation\nPhase / parameter / DB function\nArguments + dependencies + objective\nSnowflake CONTRACT_REVISIONS", size=18)
    p.line([(380, 225), (500, 225)], "Commands", (383, 184, 112))
    p.line([(1110, 225), (980, 225)], "Pinned spec", (987, 184, 117), dashed=True)
    p.frame(45, 397, 565, 215, "CORE PHASE · BRONZE INPUT", BRONZE)
    p.box(60, 435, 225, 160, "BRONZE\nInput model\nSaved raw snapshot", BRONZE, size=19)
    p.box(335, 435, 260, 160, "Core Calculation\nValidate + map\nCalculate every param", size=18)
    p.box(645, 435, 240, 160, "SILVER\nImmutable core\nComplete model", GRAY, size=19)
    p.box(935, 435, 275, 160, "Custom Calculation\nSilver + input overrides\nSelected parameters", size=18)
    p.box(1260, 435, 280, 160, "GOLD\nCalculated variation\nVersioned results", AMBER, size=19)
    for a, b in [(285, 335), (595, 645), (885, 935), (1210, 1260)]:
        p.line([(a, 515), (b, 515)], color=GREEN)
    p.line([(570, 300), (570, 320), (255, 320), (255, 435)], dashed=True)
    p.text(165, 343, 180, 30, "Store input", 16)
    p.line([(680, 300), (680, 355), (465, 355), (465, 435)], dashed=True)
    p.text(370, 326, 240, 28, "Core command", 16)
    p.line([(820, 300), (820, 355), (1072, 355), (1072, 435)], dashed=True)
    p.text(906, 326, 300, 28, "Custom command", 16)
    p.text(1242, 627, 310, 100, "Every successful custom run\ndelivers another Gold model.\nThe user processes it further\nor finishes the pipeline.", 17, color=AMBER)
    p.frame(210, 750, 1230, 265, "SHARED RUNTIME")
    p.box(260, 800, 430, 160, "Python worker on Fargate (target)\nSQS dispatch + fenced run lease\nResolve dependencies + batch calls\nAssemble the complete output", size=18)
    p.box(980, 800, 410, 160, "Snowflake (emulated locally)\nExecute released SQL functions\nStore models, runs, lineage and audit", GREEN, size=18)
    p.line([(465, 612), (465, 800)], dashed=True)
    p.text(327, 668, 276, 35, "Execute core plan", 17)
    p.line([(1072, 595), (1072, 660), (735, 660), (735, 820), (690, 820)], dashed=True)
    p.text(773, 626, 290, 33, "Execute custom plan", 17)
    p.line([(690, 865), (980, 865)], "Batched SQL calls", (714, 823, 241))
    p.line([(980, 930), (690, 930)], "Values + query IDs", (714, 938, 241), color=GREEN)
    p.text(55, 1040, 1490, 28, "Bronze, Silver and Gold are logical data layers; one database can host all three.", 18, True)
    p.text(55, 1080, 1490, 28, "Solid green = model data. Dashed blue = control/specification. Each run records inputs, contract, functions, outputs and audit.", 17)
    return p


def contract():
    p = Page("02 Data Contract", "Data Contract · from specification to DB execution",
             "Revision 3 target · Python reads a published contract at command time; workers use the saved plan, including on retry.", 1600, 1130)
    p.box(80, 155, 430, 250, "Data Contract stored in Snowflake\nCONTRACT_DRAFTS: optimistic edits\nCONTRACT_REVISIONS: immutable\nParameter schema + phase bindings\nArgument sources + dependencies\nObjective + allowed overrides\nID / revision / canonical hash", size=17)
    p.box(630, 155, 370, 250, "Python contract resolver\nCheck function signatures\nCheck dependency graph\nResolve phase + process settings\nValidate all required bindings\nPin exact published versions", size=18)
    p.box(1120, 155, 400, 250, "Frozen plan persisted in RUNS\nContract ID + revision + hash\nFunction releases + SQL names\nSource + scenario revisions\nArguments, order and planHash", size=18)
    p.line([(510, 280), (630, 280)], "Resolve", (516, 235, 108))
    p.line([(1000, 280), (1120, 280)], "Freeze", (1006, 235, 108))
    p.box(630, 530, 370, 150, "SQL function catalogue\nEditable drafts → published releases\nID + revision → DB SQL name\nStored in Snowflake / emulator", GREEN, size=17)
    p.line([(815, 530), (815, 405)], dashed=True, color=GREEN)
    p.text(659, 451, 313, 34, "Resolve released DB functions", 17, color=GREEN)
    p.line([(1320, 405), (1320, 730), (295, 730), (295, 790)], dashed=True)
    p.text(414, 700, 665, 30, "Worker verifies saved planHash; retry never resolves latest", 18)
    p.box(80, 790, 430, 150, "Python calculation runtime\nMap argument values\nExecute dependency waves\nBatch calls by function release", size=18)
    p.box(630, 790, 370, 150, "Database execution\nEvaluate SQL functions\nCompute the SQL objective\nReturn values + query IDs", GREEN, size=18)
    p.box(1120, 790, 400, 150, "Silver or Gold output\nEvery parameter present\nFunction versions in lineage\nResult + audit/outboxes + success", AMBER, size=18)
    p.line([(510, 865), (630, 865)], "SQL", (516, 823, 108))
    p.line([(1000, 865), (1120, 865)], "Persist", (1006, 823, 108), color=GREEN)
    p.frame(80, 995, 1440, 95, "ONE PARAMETER EXAMPLE")
    p.text(105, 1010, 1390, 62, "CriticalIllness → CUSTOM_CI @ revision 1 → INSURANCE.RELEASES.CUSTOM_CI_R1(125000.00, 0.90)\nSQL body: ROUND(AMOUNT * FACTOR, 2)    |    Returned value: 112500.00", 18)
    return p


def regional():
    p = Page("03 Silver to Gold", "Custom runs · override input parameters from Silver",
             "Revision 3 target · POST /runs returns 202; Vue polls saved status; fenced commits protect immutable results.", 1600, 1220)
    p.box(580, 150, 430, 135, "SILVER · Immutable core model\nCompleted Core Calculation output\n{ DeathPenatly: 800 }", GRAY, size=20)
    p.box(580, 375, 430, 190, "Editable custom scenario\nSource: the immutable core model\nUser changes DeathPenatly to 700\nEach edit saves a new scenario revision", size=19)
    p.box(80, 375, 330, 190, "Vue workspace\nEdit model values / settings\nSave next draft revision\nRun or retry anytime", size=18)
    p.box(1160, 375, 360, 190, "Pinned Data Contract\nSchema + custom process rules\nReused or custom SQL releases\nAllowed override rules", size=18)
    p.line([(795, 285), (795, 375)], color=GREEN)
    p.text(617, 311, 356, 33, "Reference; never modify Silver", 17, color=GREEN)
    p.line([(410, 470), (580, 470)], "Edit / run", (420, 430, 150))
    p.line([(1160, 470), (1010, 470)], "Resolve spec", (1018, 430, 134), dashed=True)
    p.box(580, 660, 430, 140, "Queued custom run · Regional\nFresh Silver + draft overrides\nFenced worker → DB function calls", size=19)
    p.line([(795, 565), (795, 660)])
    p.text(617, 600, 356, 30, "New run for a new draft", 17)
    p.box(580, 920, 430, 150, "GOLD · Completed regional models\nFirst calculation: { DeathPenatly: 630 }\nNext calculation: { DeathPenatly: 540 }\nPrior results remain readable", AMBER, size=18)
    p.line([(795, 800), (795, 920)], color=GREEN)
    p.text(617, 850, 356, 31, "Commit result + lineage + audit", 17, color=GREEN)
    p.box(1160, 660, 360, 195, "Retry the same pinned run\nSame input + function versions\nSucceeded → return saved result\nFailed → retry frozen input\nNever use partial output", size=17)
    p.line([(1010, 708), (1160, 708)], "Retry", (1020, 670, 130))
    p.line([(1160, 810), (1090, 810), (1090, 777), (1010, 777)], dashed=True)
    p.box(80, 665, 330, 210, "DeathPenatly example\nImmutable core amount: 800\nFirst user edit: 700 → result 630\nNext user edit: 600 → result 540\nRegional rule: reduce by 10%", GREEN, size=16)
    p.text(75, 890, 345, 75, "Recalculation does not\ncompound previous Gold values.", 18, True, color=GREEN)
    p.line([(580, 995), (45, 995), (45, 470), (80, 470)], color=GREEN)
    p.text(85, 1007, 425, 44, "Review Gold; finish or edit the next draft", 17, color=GREEN)
    p.text(1135, 921, 410, 118, "Failure publishes no Gold result.\nPersist attempt errors; retain history.\nGuard result pointer by draft + run.\nNew settings need a new draft/run.", 18)
    p.text(55, 1110, 1490, 34, "After every delivered Gold model the user decides: finish the pipeline, edit the draft and rerun from Silver, or chain a new scenario from this Gold.", 18, True)
    p.text(55, 1155, 1490, 34, "Default source = Silver. Chaining must explicitly pin the parent Gold ID/revision/hash; it never replaces Silver or rewrites history.", 18)
    return p


# --- Implemented runtime the layers execute on ------------------------------

def components():
    p = Page("04 Components", "Runtime components · local integration environment",
             "Implemented version 2 · synchronous local API. Proposed AWS process split is on page 8; dashed = optional / generation.")
    p.frame(40, 140, 395, 925, "CLIENT EXAMPLE / CONTRACT")
    p.frame(475, 140, 465, 925, "LITESTAR API PROCESS · 8000")
    p.frame(980, 140, 420, 520, "SNOWFLAKE EMULATOR · 8084")
    p.frame(980, 735, 420, 330, "OPTIONAL · audit PROFILE", AMBER, True)
    p.box(65, 190, 345, 105, "api/openapi.yaml\nMaintained API contract\nGET /openapi.yaml", GRAY)
    p.box(65, 370, 345, 90, "openapi-typescript\nGenerate src/api/generated.ts", GRAY, dashed=True, size=17)
    p.box(65, 540, 345, 110, "Vue client example\nuseInsuranceCalculation\nopenapi-fetch + decimal strings", BLUE, size=17)
    p.box(65, 770, 345, 195, "Current frontend status\nClient + composable supplied\nGenerated types are not bundled\nNo runnable Vue app or proxy\nis included in this package", GRAY, size=17)
    p.line([(238, 295), (238, 370)], "generation command", (88, 320, 300), True, color=GRAY)
    p.line([(238, 460), (238, 540)], "generated paths / DTO types", (73, 483, 330), True, color=GRAY)
    p.box(500, 190, 415, 100, "Litestar routes · app.py\nrequestId / expectedRevision\nSynchronous HTTP operations", size=18)
    p.box(500, 335, 195, 125, "FunctionCatalog\nConfigCatalog\nDrafts and releases\nPinned bindings", size=16)
    p.box(720, 335, 195, 125, "ModelLifecycle\ncore / fork / change\ncalculate", size=17)
    p.box(500, 520, 195, 110, "Repository\nModels + revisions\nAudit + outbox", size=17)
    p.box(720, 520, 195, 110, "CalculationWorker\nrun()\nDependency waves", size=16)
    p.box(500, 710, 415, 90, "SnowflakeGateway\nrows / execute_batch / objective / audit", size=17)
    p.box(500, 855, 415, 80, "Snowflake Python connector\nSession, cursor.execute(), commit()", size=17)
    p.text(497, 970, 420, 62, "One API process, one request lock.\nThe worker runs inside this process.", 17)
    p.line([(410, 595), (453, 595), (453, 240), (500, 240)])
    p.text(298, 662, 130, 28, "HTTP / JSON", 16, True)
    p.line([(600, 290), (600, 335)])
    p.line([(817, 290), (817, 335)])
    p.line([(817, 460), (817, 520)])
    p.line([(720, 397), (710, 397), (710, 575), (695, 575)])
    p.line([(500, 397), (490, 397), (490, 755), (500, 755)])
    p.line([(597, 630), (597, 710)])
    p.line([(817, 630), (817, 710)])
    p.line([(708, 800), (708, 855)], "same DB session", (548, 811, 320))
    p.box(1005, 190, 370, 95, "Snowflake HTTP endpoints\nLogin / query / session\nStarlette + Uvicorn", size=18)
    p.box(1005, 350, 370, 95, "Engine.compile() · SQLGlot\nSnowflake SQL to DuckDB SQL\nTyped SQL UDFs to SQL macros", size=17)
    p.box(1005, 505, 370, 115, "DuckDB: /data/emulator.duckdb\nSQL macros + catalogue\nModels + revisions + audit/outbox", GREEN, size=17)
    p.line([(1190, 285), (1190, 350)])
    p.line([(1190, 445), (1190, 505)])
    p.line([(915, 895), (958, 895), (958, 237), (1005, 237)])
    p.text(992, 672, 396, 38, "Query history: query IDs + JSONL log", 16)
    p.box(1005, 780, 370, 85, "audit-export process\nSQL outbox polling + psycopg", AMBER, size=18)
    p.box(1005, 960, 370, 75, "PostgreSQL · calc.AuditEvent\nAppend-only, one row per event", AMBER, size=18)
    p.line([(1190, 865), (1190, 960)], "INSERT … ON CONFLICT DO NOTHING", (1000, 895, 380), color=AMBER)
    p.line([(1375, 822), (1420, 822), (1420, 237), (1375, 237)], dashed=True, color=AMBER)
    p.text(40, 1090, 1350, 42, "The exporter reads through the Snowflake connector and writes to PostgreSQL. Cloud Snowflake, AWS and a distributed queue are not deployed here.", 16)
    return p


def functions():
    p = Page("05 Functions and bindings", "Editable SQL functions and runtime binding selection",
             "Implemented version 2 · SQL releases stay in the database. The target contract publisher validates these same releases.", height=1370)
    p.box(50, 160, 600, 110, "PUT /functions/CUSTOM_CI\nFunctionCatalog.edit(expectedRevision, definition)\nFUNCTION_DRAFTS: mutable draft r1", size=19)
    p.box(815, 160, 570, 135, "Draft SQL definition\nAMOUNT NUMBER(18,2), FACTOR NUMBER(18,8)\nRETURNS NUMBER(18,2)\nROUND(AMOUNT * FACTOR, 2)", size=18)
    p.line([(650, 215), (815, 215)], arrow=False, color=GRAY)
    p.box(50, 370, 600, 100, "POST /functions/CUSTOM_CI/preview\nCreate unique preview UDF → SELECT → DROP\nPreview(125000.00, 0.90) = 112500.00", size=18)
    p.line([(350, 270), (350, 370)], "optional preview in the SQL engine", (77, 300, 550), True, color=GRAY)
    p.box(50, 570, 600, 150, "POST /functions/CUSTOM_CI/publish\nFunctionCatalog.publish(expectedRevision: 1)\nCREATE FUNCTION INSURANCE.RELEASES.CUSTOM_CI_R1\nthen INSERT FUNCTION_RELEASES metadata", GREEN, size=17)
    p.line([(350, 470), (350, 570)], "publish exact draft revision", (77, 500, 550))
    p.box(815, 540, 570, 180, "Published release is immutable\nSQL name + signature + body + definitionHash\n\nEditing draft r2 can publish CUSTOM_CI_R2.\nExisting bindings still use CUSTOM_CI_R1.\nDDL publication precedes registry insertion.", GREEN, size=18)
    p.line([(650, 645), (815, 645)], arrow=False, color=GREEN)
    p.box(50, 815, 600, 180, "Select the release for CriticalIllness\nfunctionId: CUSTOM_CI · functionRevision: 1\narguments: param CriticalIllness, constant factor\nconstants.factor: \"0.90\" · dependsOn: []\nConfigCatalog.resolve_bindings() embeds the release", size=18)
    p.line([(350, 720), (350, 815)], "published release lookup", (77, 750, 550))
    p.box(815, 815, 570, 180, "Persist the binding in one of two places\nCONFIG_REVISIONS for a reusable regional profile\nor settings.bindings in a variation snapshot\n\nA binding override affects only selected parameters.\nA missing variation binding preserves the value.", size=18)
    p.line([(650, 905), (815, 905)])
    p.box(50, 1120, 600, 145, "Calculate using pinned settings\nConfigCatalog.get() for a new core\nSaved variation settings for a later calculation\nworker_bindings(settings) → CalculationWorker.run()", size=18)
    p.line([(350, 995), (350, 1120)], "read the saved effective configuration", (52, 1035, 595))
    p.box(815, 1120, 570, 145, "SnowflakeGateway.execute_batch()\nSELECT CALL_ID, CUSTOM_CI_R1(AMOUNT, FACTOR)\nConnector → emulator → DuckDB SQL macro\n125000.00 × 0.90 = 112500.00", GREEN, size=18)
    p.line([(650, 1192), (815, 1192)])
    p.text(40, 1310, 1360, 38, "All names above are actual classes/tables or concrete example releases. Function formulas execute inside the database engine.", 17)
    return p


def calculation():
    p = Page("06 Calculation sequence", "Calculation sequence · one Core Calculation request",
             "Implemented version 2 · synchronous request. Regional writes advance the head and append a historical revision.", 1500, 1550)
    xs = [150, 450, 750, 1050, 1350]
    ui, api, w, g, db = xs
    p.participants(list(zip(xs, ["Frontend / HTTP client", "Litestar + ModelLifecycle", "CalculationWorker", "SnowflakeGateway\n+ Python connector", "Local Snowflake emulator\n+ DuckDB SQL engine"])), 1455)
    p.activation(api, 263, 1217)
    p.activation(w, 680, 495)
    p.message(ui, api, 270, "POST /core-models")
    p.message(api, g, 330, "BEGIN; requestId replay check")
    p.message(g, db, 380, "SQL: transaction + receipt lookup")
    p.message(db, g, 430, "No cached command result", True)
    p.message(api, g, 480, "ConfigCatalog.get() uses rows() for core config r1")
    p.message(g, db, 530, "SELECT CONFIG_REVISIONS")
    p.message(db, g, 580, "Pinned function releases + mappings", True)
    p.message(g, api, 630, "Complete effective settings", True)
    p.message(api, w, 680, "run(policies, bindings, objective)")
    # Loop is an ordinary UML combined fragment; draw it around wave messages.
    p.frame(625, 724, 815, 205, "loop [dependency waves / function batches]", GRAY)
    p.message(w, g, 775, "execute_batch(function_name, calls)")
    p.message(g, db, 825, "SELECT CALL_ID, SQL_FUNCTION(args)")
    p.message(db, g, 875, "Rows + sfqid", True)
    p.message(g, w, 915, "Values correlated by CALL_ID", True)
    p.message(w, g, 995, "objective(); audit(records)")
    p.message(g, db, 1040, "SQL SUM(UDF(...)); INSERT trace")
    p.message(db, g, 1085, "Objective totals; trace staged", True)
    p.message(g, w, 1125, "Objective values / write ack", True)
    p.message(w, api, 1175, "Results + iterations", True)
    p.message(api, g, 1230, "Repository uses rows(): model / event / receipt")
    p.message(g, db, 1280, "INSERT model + audit + outbox + receipt")
    p.message(api, g, 1330, "connection.commit()")
    p.message(g, db, 1370, "COMMIT")
    p.message(db, g, 1410, "Committed", True)
    p.message(g, api, 1445, "Commit acknowledged", True)
    p.message(api, ui, 1480, "201: immutable core JSON + contentHash", True)
    p.text(40, 1510, 1420, 30, "On failure: rollback. A replay returns the saved response without re-running SQL formulas. Core functions cover every input parameter.", 15)
    return p


def audit():
    p = Page("07 Audit sequence", "Audit persistence and the PostgreSQL copy",
             "Implemented · SQL outbox → audit-export → PostgreSQL (calc.AuditEvent). Proposed completion events use a separate outbox.", 1440, 1220)
    api, db, exporter, aws = 190, 545, 895, 1250
    p.participants([(api, "Litestar + Repository"), (db, "Emulator + SQL tables\nvia Snowflake connector"), (exporter, "audit-export process"), (aws, "PostgreSQL\ncalc.AuditEvent")], 1100)
    p.message(api, db, 280, "BEGIN; model / revision / audit / outbox")
    p.message(api, db, 335, "Save command receipt; COMMIT")
    p.message(db, api, 390, "Atomic commit succeeded", True)
    p.message(exporter, db, 470, "SELECT pending outbox JOIN audit events")
    p.message(db, exporter, 525, "Committed event payloads", True)
    p.frame(470, 585, 910, 450, "loop [each pending event]", AMBER)
    p.message(exporter, aws, 640, "INSERT … ON CONFLICT DO NOTHING")
    p.message(aws, exporter, 705, "Stored, or event id already there", True)
    p.message(exporter, aws, 805, "If already there: compare SHA-256")
    p.message(aws, exporter, 860, "Same: continue · different: error", True)
    p.message(exporter, db, 945, "UPDATE AUDIT_OUTBOX delivered = true")
    p.message(db, exporter, 1005, "Delivery acknowledged", True)
    p.text(75, 1052, 590, 80, "The SQL model transaction never waits\nfor the PostgreSQL copy.", 18, True, color=GREEN)
    p.text(750, 1060, 630, 105, "Failure or crash before the delivery flag:\nleave pending and retry. Identical duplicate writes\nare accepted; conflicting content raises an error.", 17)
    p.text(40, 1170, 1360, 28, "Calculation audit: before/after values, bindings, arguments, objective and query IDs. Emulator query history also records failed queries.", 16)
    return p


def aws_orchestration():
    p = Page("08 Contract storage and AWS orchestration",
             "Contract storage and run orchestration in AWS",
             "Revision 3 target · not deployed locally. Python interprets contracts; Snowflake stores state and executes SQL formulas.",
             1800, 1750)
    p.frame(35, 145, 1730, 265, "PUBLISH BEFORE RUN ACCEPTANCE")
    p.box(60, 180, 420, 190,
          "Authoring: Vue admin or optional Git CI\nPUT /contracts/{id}\nPOST /contracts/{id}/publish\nCONTRACT_DRAFTS + expectedRevision\nMutable authoring; exact revision to publish", size=17)
    p.box(620, 180, 520, 190,
          "Python publication validation\nSchema + dependencies + argument types\nExact FUNCTION_RELEASES and signatures\nObjective + allowed input overrides\nNo function DDL in publication transaction", size=18)
    p.box(1280, 180, 460, 190,
          "Snowflake CONTRACT_REVISIONS\nImmutable ID / revision / hash / DOCUMENT\nPublisher identity + publication time\nDeprecation stored separately\nRuntime source of truth", GREEN, size=17)
    p.line([(480, 275), (620, 275)], "Validate", (485, 230, 130))
    p.line([(1140, 275), (1280, 275)], "Publish", (1145, 230, 130), color=GREEN)
    p.line([(1510, 370), (1510, 445), (880, 445), (880, 525)], dashed=True, color=GREEN)
    p.text(922, 421, 450, 30, "Read exact revision; verify canonical hash", 16, color=GREEN)

    p.box(60, 525, 420, 210,
          "1 · Vue → Litestar command API\nPOST /runs with requestId\nCore: retained Bronze reference\nRegional: scenario + expectedRevision\nReceipt check before selecting latest\n202 + runId + Location after commit", size=18)
    p.box(620, 525, 520, 210,
          "2 · Accept atomically in Snowflake\nPython freezes input + plan + planHash\nRUNS = QUEUED + command receipt\nRUN_OUTBOX = pending dispatch\nContract/source/scenario revisions pinned\nNo direct API-to-queue dual write", GREEN, size=18)
    p.box(1280, 525, 460, 210,
          "3 · Run relay → SQS FIFO\nRead pending RUN_OUTBOX\nSend runId + dispatchId + generation\nGroup = scenarioId (core: bronzeId)\nDeduplication ID = dispatchId\nMark delivered only after send ACK", size=17)
    p.line([(480, 625), (620, 625)], "Accept", (485, 580, 130))
    p.line([(1140, 625), (1280, 625)], "Relay polls", (1145, 580, 130))

    p.box(1280, 890, 460, 225,
          "4 · Separate ECS Fargate worker\nConditional claim + fenced lease\nCommit RUN_ATTEMPTS start record\nLoad frozen plan; verify planHash\nHeartbeat lease + extend SQS visibility\nSynchronous batched SQL functions\nDelete queue message after result commit", size=17)
    p.line([(1510, 735), (1510, 890)], "Receive dispatch", (1315, 797, 390))
    p.box(620, 890, 520, 225,
          "5 · Atomic result transaction in Snowflake\nRecheck run owner / fencing token / lease\nComplete Silver or Gold + lineage + traces\nAUDIT_EVENTS + AUDIT_OUTBOX\nRUN_EVENT_OUTBOX + attempt success\nRUNS = SUCCEEDED + result reference\nGuard scenario pointer by draft + selected run", GREEN, size=17)
    p.line([(1280, 1000), (1140, 1000)], "Commit", (1145, 955, 130), color=GREEN)
    p.box(60, 890, 420, 225,
          "6 · Vue listens through persisted state\nGET /runs/{runId}\nETag / If-None-Match polling\nSSE can be added later\nSuccess → immutable result reference\nFailure → diagnostics + retry eligibility\nReview Gold; finish, edit or chain", size=17)
    p.line([(620, 1000), (480, 1000)], "Via API", (485, 955, 130), color=GREEN)
    p.line([(270, 890), (270, 735)], dashed=True)
    p.text(75, 791, 390, 35, "New draft → new run; retry → same plan", 16)

    p.box(60, 1280, 420, 240,
          "Failure and recovery\nRollback: no partial Silver or Gold\nPersist attempt error and retry budget\nRETRYABLE → redelivery / backoff\nExpired lease → fence + recovery dispatch\nDLQ reconciler → terminal FAILED\nManual retry → new dispatch generation\nOld generations cannot claim or publish", AMBER, size=17)
    p.box(620, 1280, 520, 240,
          "Independent downstream outboxes\nAUDIT_OUTBOX → audit-export → PostgreSQL\nExisting direct copy; one row per event id\n\nRUN_EVENT_OUTBOX → relay → EventBridge\nProposed RunCompleted + result reference\nACK each successful entry; retry failures\nConsumers deduplicate by stable event ID", AMBER, size=17)
    p.line([(880, 1115), (880, 1280)], "Read only committed events", (665, 1175, 430), color=AMBER)
    p.box(1280, 1280, 460, 240,
          "Stage 2 · when multi-step state is needed\nStep Functions Standard OR Temporal\nCore → human wait → custom fan-out\nExplicit chaining from delivered Gold\nActivities call the same idempotent run API\nServer-side callback / workflow state\nChoose one retry owner per layer\nNo engine is deployed in this package", GRAY, dashed=True, size=17)

    p.text(55, 1570, 1690, 38,
           "FIFO deduplication lasts five minutes. Relay retries reuse dispatchId; deliberate retry/recovery uses a new dispatchId and generation.", 18, True)
    p.text(55, 1620, 1690, 38,
           "Queue order is not database ownership. Prove control-table uniqueness and concurrency; fence stale workers and guard scenario result pointers.", 18)
    p.text(55, 1670, 1690, 38,
           "AWS deployment: API / relay / worker credentials via Secrets Manager; Snowflake region alignment and PrivateLink where required and supported.", 17)
    return p


def business_message(p, a, b, y, label, reply=False):
    p.line([(a, y), (b, y)], dashed=reply, color=GREEN if reply else BLUE)
    width = abs(a - b) - 20
    lines = []
    for paragraph in label.split("\n"):
        lines.extend(textwrap.wrap(paragraph, max(18, int(width / 8.1)),
                                   break_long_words=False, break_on_hyphens=False))
    height = 24 * len(lines)
    p.text(min(a, b) + 10, y - height - 5, width, height, "\n".join(lines), 16)


def parameter_sequence():
    p = Page("09 Core and custom modeling inputs",
             "Core uses DataContract. Custom runs use DataContract AND Custom Configuration.",
             "Core models Bronze into Silver. Each new custom calculation loads both independent definitions before processing its source model. Proposed design.",
             2355, 2910)
    p.box(25,310,2305,820,color=BRONZE,fill="#F5E3D0")
    p.box(25,1150,2305,235,color=GRAY,fill="#E7EBF0")
    p.box(25,1410,2305,1095,color=BLUE,fill="#EAF2FF")
    p.box(25,2525,2305,265,color=AMBER,fill="#FFF0B8")
    user, python, contract, config, worker, database = [190,585,980,1375,1770,2165]
    entries = [
        (user,"Business user","Vue application",BLUE),
        (python,"Python modeling service","Litestar API · plan resolver",BLUE),
        (contract,"DataContract","Snowflake · CONTRACT_REVISIONS","#6E57A5"),
        (config,"Custom Configuration","Snowflake · CONFIG_REVISIONS","#A65382"),
        (worker,"Calculation worker","Python · Amazon ECS / Fargate",BLUE),
        (database,"Models and SQL formulas","Snowflake · snapshots / SQL",GREEN),
    ]
    for x,business,technical,color in entries:
        p.box(x-178,145,356,120,color=color)
        p.text(x-165,158,330,45,business,20,True)
        p.text(x-165,212,330,34,technical,17,color=color)
        p.line([(x,265),(x,2800)],dashed=True,arrow=False,color=GRAY)
    p.text(48,323,2240,38,"1 · CORE PHASE — DataContract defines the parameters used to model Bronze into Silver",23,True,"left",BRONZE)
    business_message(p,user,python,435,"Start Core Calculation\nSelect the Bronze model")
    business_message(p,python,contract,505,"Read Core parameter definitions\nand source mappings")
    business_message(p,contract,python,595,"DeathPenatly; Bronze source field;\ndecimal type; Core cap 800",True)
    business_message(p,python,database,680,"Read Bronze values using the parameter mappings supplied by DataContract")
    business_message(p,database,python,745,"Bronze { DeathPenatly: 1000 }",True)
    p.text(430,775,1780,54,"Python uses DataContract to extract, type and validate the model parameters and resolve Core functions.\nThe example maps the Bronze field DeathPenatly to the calculation parameter DeathPenatly.",18,True,"left")
    business_message(p,python,database,885,"Save frozen Bronze input + DataContract version/hash + Core execution plan + queued run")
    business_message(p,python,user,955,"Core run accepted\nFollow its saved status",True)
    p.frame(395,995,1525,70,"ref · BACKGROUND DISPATCH — page 11",BLUE)
    p.text(415,1012,1485,39,"Saved run → Python dispatch service → Amazon SQS FIFO → Python worker; load the frozen Core plan.",17)
    business_message(p,worker,database,1110,"Execute Core SQL\nLEAST(1000, 800) = 800")
    p.text(48,1161,2240,36,"SILVER · completed Core model",22,True,"left",GRAY)
    business_message(p,database,worker,1235,"Core output { DeathPenatly: 800 }",True)
    business_message(p,worker,database,1300,"Commit immutable Silver + Ready")
    p.text(80,1320,2195,48,"Vue polls Litestar for the saved outcome and displays Silver { DeathPenatly: 800 }.\nA later custom calculation selects this Silver model as its source.",18,True)
    p.text(48,1423,2240,38,"2 · CUSTOM PHASE — Python requires BOTH DataContract AND Custom Configuration",23,True,"left",BLUE)
    business_message(p,user,python,1515,"Start custom calculation\nSelect Silver + both references")
    p.frame(420,1560,1110,370,"par · INDEPENDENT READS — neither read depends on the other",BLUE)
    business_message(p,python,contract,1650,"Read DataContract for this new run")
    business_message(p,contract,python,1715,"Parameter definitions, types\nand permitted overrides",True)
    p.line([(420,1745),(1530,1745)],dashed=True,arrow=False,color=GRAY)
    business_message(p,python,config,1815,"Read the selected Custom Configuration")
    business_message(p,config,python,1895,"Regional Calculation; DeathPenatly override 700; factor 0.90; released custom SQL",True)
    p.text(430,1950,1780,60,"JOIN · wait for BOTH reads. Python checks configuration compatibility with DataContract.\nAn invalid or missing input stops acceptance; neither document alone is the complete custom plan.",19,True,"left")
    business_message(p,python,database,2085,"Read the selected Silver source model")
    business_message(p,database,python,2150,"Silver { DeathPenatly: 800 }",True)
    p.text(430,2180,1780,60,"Python combines source + DataContract + Custom Configuration.\nConfigured override replaces 800 with 700 for this run; the stored Silver model remains 800.",19,True,"left")
    business_message(p,python,database,2310,"Save frozen input 700 + BOTH document revisions/hashes + resolved custom plan + queued run")
    business_message(p,python,user,2375,"Custom run accepted\nFollow this run's saved status",True)
    p.frame(395,2415,1525,65,"ref · SAME BACKGROUND DISPATCH — page 11",BLUE)
    p.text(415,2433,1485,34,"Python dispatch service → Amazon SQS FIFO → worker; load the complete frozen custom plan.",17)
    p.text(48,2536,2240,38,"GOLD · new custom result, with source / DataContract / Custom Configuration lineage",22,True,"left",AMBER)
    business_message(p,worker,database,2630,"Execute configured custom SQL\n700 × 0.90 = 630")
    business_message(p,database,worker,2690,"Custom output { DeathPenatly: 630 }",True)
    business_message(p,worker,database,2750,"Commit new Gold + Ready")
    p.text(50,2820,2255,32,"Every NEW custom calculation reads both documents independently. A RETRY reuses the same frozen input, DataContract, Custom Configuration and plan.",18,True)
    p.text(50,2860,2255,32,"DataContract supplies the model definition and Core rules. Custom Configuration supplies the selected custom process, overrides and calculation settings.",18)
    return p


def assessment_worker():
    p = Page("10 The business calculation journey",
             "Core → Silver, then custom processes · deployment responsibilities",
             "The background worker organises the work. Snowflake performs the arithmetic. The application shows the saved outcome.",
             1680, 1420)
    p.box(50, 145, 480, 155,
          "BRONZE · Original policy model\n{ DeathPenatly: 1000 }\nCore rule: cap the amount at 800", BRONZE, fill="#F5E3D0", size=22)
    p.box(600, 145, 480, 155,
          "SILVER · Immutable core model\n{ DeathPenatly: 800 }\nCore phase output; immutable", GRAY, fill="#E7EBF0", size=22)
    p.box(1150, 145, 480, 155,
          "CUSTOM INPUT · Regional example\n{ DeathPenatly: 700 }\nUser overrides DeathPenatly to 700", BLUE, fill="#EAF2FF", size=22)
    p.line([(530, 222), (600, 222)], color=GRAY)
    p.line([(1080, 222), (1150, 222)])
    p.text(50, 365, 1580, 38, "AFTER CORE SUCCEEDS · THE USER SELECTS A CUSTOM PROCESS (EXAMPLE: REGIONAL)", 22, True, "left")
    p.box(50, 455, 350, 180,
          "User clicks Calculate\nSilver + input override:\n{ DeathPenatly: 700 }\nThe screen remains usable", size=21)
    p.box(540, 455, 500, 180,
          "Calculation application · Litestar\nRead the published Data Contract\nValidate and save the fixed input and rules\nReturn a reference and show Waiting\nFull actor sequence: page 11", size=20)
    p.box(1180, 455, 450, 180,
          "Amazon Simple Queue Service\nBackground job waiting list hosted in AWS\nHolds saved calculation references\nA worker starts when it picks one up", size=20)
    p.line([(400, 545), (540, 545)], "Request", (409, 500, 122))
    p.line([(1040, 545), (1180, 545)], "Queue work", (1049, 500, 122))
    p.box(50, 805, 350, 235,
          "The screen follows progress\nWaiting → Calculating\nAutomatically checks saved status\nClosing the browser does not\nstop the background calculation\nReturn later to see the outcome", size=19)
    p.box(540, 765, 500, 295,
          "PYTHON CALCULATION WORKER\nSeparate container running on AWS Fargate\n\n1. Receive a request from the Amazon queue\n2. Load its fixed model and rules\n3. Use CalculationWorker.run() to call Snowflake\n4. Save the result or failure and finish the job", size=18)
    p.box(1180, 765, 450, 295,
          "SNOWFLAKE · CALCULATES THE VALUE\nInput: { DeathPenatly: 700 }\n\nRegional rule: reduce the amount by 10%\n700 × 0.90 = 630\n\nReturn 630, or a calculation error", GREEN, fill="#E8F4EB", size=20)
    p.line([(1405, 635), (1405, 700), (790, 700), (790, 765)])
    p.text(540, 654, 1020, 34, "A separate Python delivery process forwards saved requests from Snowflake into the Amazon queue.", 17)
    p.text(935, 718, 650, 34, "An available Python worker receives the queued request", 18)
    p.line([(1040, 860), (1180, 860)], "Calculate", (1049, 815, 122))
    p.line([(1180, 980), (1040, 980)], "Value / error", (1049, 935, 122), dashed=True, color=GREEN)
    p.line([(790, 1060), (790, 1120), (410, 1120), (410, 1180)], color=GREEN)
    p.line([(790, 1120), (1210, 1120), (1210, 1180)], color="#B04444")
    p.box(50, 1180, 720, 155,
          "SUCCESS · save GOLD and mark Ready together\n{ DeathPenatly: 630 }\nThe application displays the completed regional model", AMBER, fill="#FFF0B8", size=22)
    p.box(850, 1180, 780, 155,
          "FAILURE · save the reason and mark Failed\nThe application shows what failed and the available retry\nPreserve the core model and all earlier completed results", "#B04444", fill="#FBEDED", size=21)
    p.text(50, 1360, 1580, 32,
           "Proposed deployment: Amazon Elastic Container Service keeps the Python worker running on AWS Fargate. This queue consumer is not implemented yet.", 17)
    return p


def calculation_participants(p, bottom):
    positions = [160, 480, 800, 1120, 1440, 1760, 2080, 2400, 2720]
    labels = [("Business user", "Vue application"),
              ("Calculation application", "Litestar API · Python resolver"),
              ("DataContract", "Published calculation rules\nSnowflake CONTRACT_REVISIONS"),
              ("Custom Configuration", "Process rules and overrides\nSnowflake CONFIG_REVISIONS"),
              ("Model and run records", "Snowflake storage"),
              ("Job dispatch service", "Python · RUN_OUTBOX relay"),
              ("Calculation queue", "Amazon Simple Queue Service\nFIFO queue · AWS"),
              ("Calculation worker", "Python · Amazon ECS/Fargate"),
              ("Formula execution", "Snowflake SQL engine")]
    for x, (business, technical) in zip(positions, labels):
        color = "#6E57A5" if business == "DataContract" else "#A65382" if business == "Custom Configuration" else BLUE
        p.box(x - 140, 145, 280, 100, color=color)
        p.text(x - 130, 151, 260, 34, business, 18, True)
        p.text(x - 130, 188, 260, 50, technical, 15 if business in ("DataContract", "Custom Configuration") else 16, color=color)
        p.line([(x, 245), (x, bottom)], dashed=True, arrow=False, color=GRAY)
    return positions


def assessment_updates():
    p = Page("11 Background execution with both configuration inputs",
             "Custom execution · read BOTH inputs, join, dispatch and report the outcome",
             "DataContract supplies model definitions. Custom Configuration supplies the selected process and overrides. Their reads are independent. Core lifecycle: page 9.",
             2880, 2970)
    screen, app, contracts, configs, records, relay, queue, worker, sql = calculation_participants(p, 2900)
    p.activation(app,345,970)
    p.text(50,280,2780,38,"ACCEPTANCE · collect both independent definitions before building the custom calculation plan",21,True,"left")
    business_message(p,screen,app,390,"Start custom calculation\nSelect source + both references")
    business_message(p,app,records,460,"Check request replay and source/scenario revision")
    business_message(p,records,app,525,"New command; selected Silver source is valid",True)
    p.frame(440,570,820,345,"par · INDEPENDENT READS",BLUE)
    business_message(p,app,contracts,650,"Read selected DataContract")
    business_message(p,contracts,app,720,"Parameter definitions and types;\npermitted custom overrides",True)
    p.line([(440,750),(1260,750)],dashed=True,arrow=False,color=GRAY)
    business_message(p,app,configs,815,"Read selected Custom Configuration")
    business_message(p,configs,app,885,"Regional process; override 700; factor 0.90; exact custom SQL",True)
    p.line([(app,980),(app+58,980),(app+58,1030),(app,1030)])
    p.text(565,950,765,100,"JOIN · wait for BOTH reads. Python validates compatibility,\nsource mappings and overrides, resolves released functions,\nthen freezes source, both documents and the effective plan.",18,color=BLUE)
    business_message(p,app,records,1150,"Commit input 700 + BOTH document revisions/hashes + plan + Waiting + receipt + dispatch instruction")
    business_message(p,records,app,1210,"Calculation safely recorded",True)
    business_message(p,app,screen,1290,"202 Accepted\nKeep this calculation reference",True)

    p.frame(35, 1345, 2810, 1540, "par [background processing and client status checks run independently]", GRAY)
    p.text(60, 1363, 1800, 34, "[BACKGROUND PROCESSING]", 18, True, "left", BLUE)
    business_message(p, relay, records, 1460, "Read pending dispatch instruction")
    business_message(p, records, relay, 1530, "Saved calculation reference", True)
    business_message(p, relay, queue, 1600, "Send calculation reference")
    business_message(p, queue, relay, 1670, "Delivery accepted", True)
    business_message(p, relay, records, 1740, "Mark dispatch delivered")
    business_message(p, worker, queue, 1810, "Wait for the next calculation")
    business_message(p, queue, worker, 1880, "Return queued calculation", True)
    business_message(p, worker, records, 1950, "Claim ownership; open attempt; mark Calculating; load frozen input and plan")
    business_message(p, records, worker, 2020, "Input 700 + frozen DataContract AND Custom Configuration + exact function release", True)
    business_message(p, worker, sql, 2100, "Execute saved regional rule\n700 × 0.90")
    business_message(p, sql, worker, 2170, "Return 630 or a calculation error", True)
    p.frame(1400, 2220, 1410, 280, "alt [calculation succeeds]", GREEN)
    business_message(p, worker, records, 2290, "Commit Gold { DeathPenatly: 630 } + both document versions + lineage + audit + Ready together")
    business_message(p, worker, queue, 2355, "Acknowledge completed job")
    p.line([(1400, 2380), (2810, 2380)], dashed=True, arrow=False, color=GRAY)
    p.text(1418, 2387, 1280, 30, "else [calculation fails: retry handling is expanded on page 12]", 17, False, "left", "#B04444")
    business_message(p, worker, records, 2470, "Roll back incomplete output; save failure reason and retry eligibility")

    p.line([(35, 2540), (2845, 2540)], dashed=True, arrow=False, color=GRAY)
    p.text(60, 2545, 2700, 34, "[CLIENT STATUS CHECKS]  Repeat while Waiting, Calculating or Retrying; stop at Ready or Failed.", 18, True, "left", BLUE)
    business_message(p, screen, app, 2630, "Automatic status check")
    business_message(p, app, records, 2690, "Read current status, result reference and retry eligibility")
    business_message(p, records, app, 2760, "Waiting / Calculating / Retrying OR Ready + result reference OR Failed + retry option", True)
    business_message(p, app, screen, 2840, "Display status; Ready: result;\nFailed: show Retry if allowed", True)
    p.text(50, 2920, 2780, 32,
           "Each NEW custom run reads BOTH documents independently. Join and validate before freezing. Retries use the saved plan and both pinned documents. Proposed design.", 17)
    return p


def calculation_retry():
    p = Page("12 Automatic and user-requested retry sequence",
             "Retry calculation · repeat the saved request, preserve its model, DataContract and Custom Configuration",
             "Example failed request: regional input { DeathPenatly: 700 }, frozen DataContract AND Custom Configuration, selected regional rule 700 × 0.90.",
             2880, 2620)
    screen, app, contracts, configs, records, relay, queue, worker, sql = calculation_participants(p, 2555)
    p.text(60, 278, 2760, 38, "AUTOMATIC RETRY · only for recoverable failures, within the saved attempt limit", 21, True, "left")
    p.frame(1390, 345, 1420, 485, "loop [temporary failure and retry allowance remains]", AMBER)
    business_message(p, worker, sql, 425, "Execute the saved rule\n700 × 0.90")
    business_message(p, sql, worker, 495, "Temporary calculation error", True)
    business_message(p, worker, records, 565, "Roll back output; record failed attempt and Retrying; consume retry allowance")
    business_message(p, worker, queue, 635, "Make message available later\nfor a retry after backoff")
    business_message(p, queue, worker, 705, "Redeliver the same calculation", True)
    business_message(p, worker, records, 775, "Claim another attempt; reload the same saved input and plan")
    p.text(55, 565, 570, 125, "A successful attempt follows page 11.\nContinue below when the issue is permanent\nor the automatic retry allowance is exhausted.", 18, color=AMBER)
    business_message(p, worker, records, 885, "Record final Failed status, explanation and manual retry eligibility")
    business_message(p, screen, app, 955, "Check calculation status")
    business_message(p, app, records, 1015, "Read saved outcome")
    business_message(p, records, app, 1075, "Failed + explanation + retry allowed", True)
    business_message(p, app, screen, 1150, "Show failure and the\nRetry calculation button", True)

    p.text(60, 1190, 2760, 38, "USER-REQUESTED RETRY · when retry is allowed: same calculation, new attempt, same model and both configuration inputs", 21, True, "left")
    business_message(p, screen, app, 1290, "Click Retry calculation\nKeep the failed request reference")
    business_message(p, app, records, 1360, "Check retry request replay, permission and current calculation state")
    business_message(p, records, app, 1430, "Failed and eligible; original input 700, BOTH original documents and the frozen plan", True)
    p.box(670, 385, 650, 115,
          "RETRY PRESERVES BOTH PINNED DOCUMENTS\nA newer DataContract or Custom Configuration\ncannot change the saved calculation.", color="#6E57A5", size=18)
    business_message(p, app, records, 1535, "Commit Waiting + new dispatch generation + bounded retry allowance + retry receipt")
    business_message(p, app, screen, 1630, "202 Accepted\nResume checking this calculation", True)
    business_message(p, relay, records, 1700, "Read new pending dispatch")
    business_message(p, records, relay, 1770, "Same calculation; new dispatch", True)
    business_message(p, relay, queue, 1840, "Send new retry dispatch; record\ndelivery after queue acceptance")
    business_message(p, worker, queue, 1910, "Receive next queued request")
    business_message(p, queue, worker, 1980, "Retry dispatch received", True)
    business_message(p, worker, records, 2050, "Claim a new attempt; reload original input 700 and frozen plan")
    business_message(p, worker, sql, 2130, "Use the original formula\n700 × 0.90")
    business_message(p, sql, worker, 2200, "630, or another failure", True)
    business_message(p, worker, records, 2270, "Save complete Gold + Ready, or save failure/retry outcome; never publish partial output")
    business_message(p, worker, queue, 2340, "After terminal outcome is saved,\nacknowledge this dispatch")
    business_message(p, screen, app, 2350, "Automatic status check")
    business_message(p, app, records, 2405, "Read latest saved outcome")
    business_message(p, records, app, 2460, "Status and result reference, or failure reason", True)
    business_message(p, app, screen, 2535, "Show completed model\nor updated failure and next action", True)
    p.text(50, 2575, 2780, 30,
           "Already successful → return saved result. Already active → keep existing attempt. Repeated retry command → no duplicate. Edited input or either changed configuration → NEW calculation.", 17)
    return p


# --- GEA module: PostgreSQL, Alembic, Litestar API and the Vue wizard --------

def gea_components():
    p = Page("13 GEA components", "GEA module · what runs where",
             "Implemented · Vue client, Litestar API on PostgreSQL, Alembic migrations. Snowflake executes the run (page 17); the relay to it is not built yet.", 1600, 1250)
    p.frame(40, 140, 400, 770, "VUE CLIENT · gea/frontend")
    p.box(65, 185, 350, 100, "gea/api/openapi.yaml\nThe contract: 27 paths, 35 operations\nGET /gea/v1/openapi.yaml", GRAY, size=16)
    p.box(65, 345, 350, 85, "generated.ts + fields.ts\nTypes of every path, the field list", GRAY, dashed=True, size=17)
    p.box(65, 490, 350, 110, "client.ts + gea.ts\nOne function per operation\nApiResult: ok + data, or error", size=17)
    p.box(65, 660, 350, 120, "useRunWizard · useJobs · useCurrentUser\nAutosave and Next: PATCH /runs/{id}\nSubmit, watch, cancel, resolve · can()", size=17)
    p.line([(240, 285), (240, 345)], "generate", (255, 299, 130), True, color=GRAY)
    p.line([(240, 430), (240, 490)], "typed calls", (255, 444, 130), True, color=GRAY)
    p.line([(240, 660), (240, 600)])
    p.text(60, 810, 360, 70, "Every call resolves to one result object.\nNothing is thrown; a failure sets\nan error state with a message.", 16)
    p.frame(480, 140, 520, 770, "LITESTAR GEA API · python -m gea_api · 8010")
    p.box(505, 185, 470, 100, "routes.py · /gea/v1\n36 handlers: HTTP in, HTTP out\nX-Request-Id on every answer", size=17)
    p.box(505, 335, 470, 110, "respond()\nAuthenticate · the caller's role and permissions\nOne transaction · the envelope { data, error }", size=17)
    p.box(505, 495, 225, 125, "services.py\n@needs: 403 by role\nIdempotency-Key · ETag\nValidate, then write", size=16)
    p.box(750, 495, 225, 125, "errors.py\nThe one error object\nSQLSTATE to HTTP\n403 · 409 · 412 · 422", size=16)
    p.box(505, 670, 470, 100, "queries.py · runconfig.py\nSQL returns API-shaped JSON\nA save updates only the columns that changed", size=17)
    p.box(505, 815, 470, 70, "db.py · psycopg 3 connection pool", size=17)
    p.line([(415, 545), (458, 545), (458, 235), (505, 235)])
    p.text(343, 617, 150, 28, "HTTPS / JSON", 15, True)
    p.line([(740, 285), (740, 335)])
    p.line([(617, 445), (617, 495)])
    p.line([(862, 445), (862, 495)], dashed=True)
    p.line([(617, 620), (617, 670)])
    p.line([(740, 770), (740, 815)])
    p.frame(1040, 140, 520, 525, "POSTGRESQL 16 · schema gea", GREEN)
    p.box(1065, 185, 470, 115, "Tables + triggers\nOne column per field of the workbook\nLifecycle and immutability enforced here", GREEN, size=17)
    p.box(1065, 350, 470, 115, "DataContract + ContractDelivery + Job\nImmutable document, SHA-256 checked\nOutbox in the same transaction, in job order", GREEN, size=17)
    p.box(1065, 515, 470, 115, "Database roles (not the users' roles)\ngea_app: the API, no UPDATE on contracts\ngea_relay: the delivery functions only", GREEN, size=17)
    p.line([(975, 850), (1018, 850), (1018, 242), (1065, 242)])
    p.text(1026, 690, 44, 26, "SQL", 15, True)
    p.frame(1040, 745, 520, 165, "DEPLOYMENT STEP · before the API starts", AMBER)
    p.box(1065, 785, 470, 100, "Alembic · gea/db/postgres\nenv.py + versions 0001 to 0007\nsql/<revision>.up.sql and .down.sql", AMBER, size=17)
    p.line([(1520, 785), (1520, 665)], "alembic upgrade head", (1312, 690, 196), color=AMBER)
    p.frame(40, 975, 960, 190, "ONE LIST OF FIELDS · the workbook's")
    p.box(65, 1015, 420, 120, "gea/spec/poc-data.json\nWorkbook sheet POC Data\n36 fields, 8 steps", GRAY, size=17)
    p.box(555, 1015, 420, 120, "gea/tools/generate.py\nfields.py · OpenAPI · JSON Schema\ngenerated.ts · fields.ts · fields.md", GRAY, size=17)
    p.line([(485, 1075), (555, 1075)], color=GRAY)
    p.frame(1040, 975, 520, 190, "NOT BUILT YET", GRAY, True)
    p.box(1065, 1015, 470, 120, "Relay to Snowflake (page 17)\nclaim → MERGE DATA_CONTRACT → EXECUTE TASK\ncopy status and log back · pass a cancel on", GRAY, dashed=True, size=16)
    p.text(40, 1190, 1520, 30, "The calculation API (pages 1 to 12) is a separate process on port 8000 with its own store. GEA shares the repository, not the runtime.", 16)
    return p


def gea_table(p, x, y, name, lines, color=BLUE, width=340):
    """One table: name in bold, then the columns that carry the keys and the rules."""
    height = 46 + 22 * len(lines)
    p.box(x, y, width, height, color=color)
    p.text(x + 14, y + 8, width - 28, 28, name, 17, True, "left")
    p.text(x + 14, y + 38, width - 28, 22 * len(lines), "\n".join(lines), 15, False, "left")
    return y + height


def gea_data_model():
    p = Page("14 GEA data model", "GEA module · PostgreSQL data model",
             "Implemented · schema gea at Alembic revision 0007. Tables and columns carry the workbook's names. Full column list: gea/db/postgres/erd.svg.", 1700, 1420)
    c1, c2, c3, c4 = 50, 470, 890, 1310
    p.frame(35, 140, 1630, 220, "LOOKUPS, USER, ROLE AND DROPDOWN VALUES", GRAY)
    gea_table(p, c1, 180, "Region · BusinessPurpose · Benefit", ["PK Id (north-america, rnd, mortality)", "Name · SortOrder · IsActive", "three lookup tables of the same shape:", "the closed lists of the project form"], GREEN)
    gea_table(p, c2, 180, "User", ["PK Id · Subject · Name · Email", "FK RegionId → Region (home region)", "FK RoleId → Role (what the user may do)"], GREEN)
    gea_table(p, c3, 180, "Role", ["PK Id · Name · SortOrder · IsActive", "CanPrepare · CanReview · CanAdminister", "viewer · preparer · reviewer · admin"], GREEN)
    gea_table(p, c4, 180, "ParameterOption", ["PK Id · Parameter · Value · Label", "SortOrder · IsDefault · IsActive", "optional FK RegionId, BusinessPurposeId,", "BenefitId · Investigation", "the values a run dropdown offers"], GRAY)
    p.line([(c2, 235), (c1 + 340, 235)], color=GREEN)
    p.line([(c4, 304), (c1 + 340, 304)], color=GRAY)
    p.line([(c2 + 340, 235), (c3, 235)], color=GREEN)

    p.frame(35, 410, 1630, 515, "PROJECT, RUN AND JOB")
    gea_table(p, c1, 450, "Project", ["PK Id · Name · State · Revision", "FK RegionId, BusinessPurposeId", "FK OwnerId → User", "Period · PeriodFrom · PeriodTo", "Description · FK ParentProjectId", "Signed off by a role that may review;", "then locked"])
    gea_table(p, c1, 678, "ProjectBenefit", ["PK ProjectId, BenefitId", "at least one, checked at commit"], width=270)
    gea_table(p, c1, 780, "Job · several runs submitted together", ["PK Id · Name · Kind · Note", "FK ProjectId (when all runs are in one)", "MaxParallel: 1 = in sequence, empty = no limit", "SubmittedBy · SubmittedAt"])
    gea_table(p, c2, 450, "Run · one column per field of workbook sheet POC Data", [
        "PK Id · FK ProjectId · Name · Treaty  (step Main)",
        "Data and set up: DataScope[] · StudyPeriodStart · StudyPeriodEnd · Investigation",
        "· StudyPeriodTreatyOverride · PerTreatyEndDatesMapping",
        "Segmentation: ExposureMethod · InitialExposureMethod · ExposureExclusion[]",
        "· PolicyTenureSegmentation · CalendarTenureSegmentation · AttainedAgeSegmentation",
        "Actuals: PartialClaimTreatment · AmountBasis",
        "IBNR: ClaimBasis · IbnrMethodology · DerivationMethod · IbnrStudyPeriodStart/End",
        "· DevelopmentFrequency · UpliftFrequency · EventMonthFilter · ReportingMonthFilter",
        "· IbnrRbnsBasis · TailStartPeriod",
        "Assigning expected: ComparisonBases · TrendAssumptions",
        "Ultimate calculation: Adjustment · UltimateRbnsBasis",
        "Actual / Expected: OutputFrequency[] · AdditionalOutputFields[]",
        "Status: draft → queued → running → complete | failed → draft · Locked · CloneSourceId",
        "CurrentContractVersion · SubmittedAt · SubmittedBy · FailureMessage · Revision",
        "FK JobId · JobOrdinal · ResolutionAction, ResolutionNote, ResolvedBy, ResolvedAt · CancelRequestedAt, By",
        "CHECK: every required field is filled before the run leaves draft"], width=760)
    gea_table(p, c4, 450, "RunStudyPeriodExclusion", ["PK RunId, Ordinal", "StartDate · EndDate", "createRun.studyPeriodExclusions", "writable only while the run is draft"])
    p.box(c4, 805, 340, 105, "NAMING\nTables and columns: PascalCase,\nquoted in SQL: gea.\"Run\".\"Treaty\"\nPK_ · FK_ · UQ_ · CK_ · IX_", GRAY, dashed=True, size=14)
    p.box(c4, 640, 340, 150, "WIZARD STEPS ARE NOT TABLES\ngea.\"RunSteps\"(runId) returns the\ncolumns grouped into the 8 steps of\nthe workbook: the steps of the contract", GRAY, dashed=True, size=15)
    p.line([(c1 + 135, 678), (c1 + 135, 650)])
    p.line([(c1 + 305, 780), (c1 + 305, 650)])
    p.line([(c2, 815), (c1 + 340, 815)])
    p.line([(c2, 520), (c1 + 340, 520)])
    p.line([(c4, 510), (c2 + 760, 510)])
    p.line([(c1 + 240, 450), (c1 + 240, 314)], color=GREEN)
    p.line([(c1 + 310, 450), (c1 + 310, 386), (c2 + 170, 386), (c2 + 170, 292)], color=GREEN)

    p.frame(35, 960, 1630, 380, "DATA CONTRACT, DELIVERY AND EXECUTION", GREEN)
    gea_table(p, c1, 1000, "CommandReceipt", ["PK CreatedBy, IdempotencyKey", "RequestHash", "ResponseStatus, Headers, Body"], GRAY)
    gea_table(p, c1, 1150, "AuditEvent", ["PK Id · AggregateType, AggregateId", "append-only"], GRAY)
    gea_table(p, c2, 1000, "DataContract", ["PK Id (contractId)", "FK RunId, ProjectId · Version", "DocumentCanonical · ContentHash", "CHECK hash = sha256(document)", "immutable: no update, no delete"], GREEN)
    gea_table(p, c3, 1000, "ContractDelivery", ["PK ContractId, Target", "Status: pending → delivering", "→ delivered | failed | cancelled", "Attempts · lease · LastError", "the outbox to Snowflake, claimed in job order"], GREEN)
    gea_table(p, c3, 1215, "RunLog", ["PK Id · FK RunId, ContractId · ExternalId", "StepKey · Level · Message · Detail", "append-only; a line is stored once"], GREEN)
    gea_table(p, c4, 1000, "RunExecution", ["PK ContractId · FK RunId", "Status reported by Snowflake"], GREEN)
    gea_table(p, c4, 1140, "RunExecutionStep", ["PK ContractId, StepKey", "Status · StartedAt · FinishedAt"], GREEN)
    p.line([(c2 + 120, 1000), (c2 + 120, 848)], color=GREEN)
    p.line([(c2 + 220, 848), (c2 + 220, 1000)], dashed=True, color=GREEN)
    p.text(c2 + 230, 931, 118, 24, "current version", 15, False, "left", GREEN)
    p.text(c2 + 24, 931, 90, 24, "published as", 15, False, "left", GREEN)
    p.line([(c3, 1070), (c2 + 340, 1070)], color=GREEN)
    p.line([(c4, 1050), (c4 - 40, 1050), (c4 - 40, 1185), (c3 - 40, 1185), (c3 - 40, 1130), (c2 + 340, 1130)], color=GREEN)
    p.line([(c4 + 170, 1140), (c4 + 170, 1090)], color=GREEN)
    p.line([(c3, 1270), (c2 + 170, 1270), (c2 + 170, 1156)], color=GREEN)
    p.text(35, 1360, 1630, 28, "An arrow points from the table that holds the foreign key to the table it references. Dashed = deferred key, checked at commit.", 16)
    return p


def gea_wizard_save():
    p = Page("15 GEA wizard save", "GEA wizard · saving the run",
             "Implemented · the wizard is saved with PATCH /runs/{id} (JSON Merge Patch of the fields that changed). If-Match guards against overwriting another tab.", 1560, 1560)
    form, wizard, api, db = 170, 540, 940, 1360
    p.participants([(form, "User in the Vue form"), (wizard, "useRunWizard\n(composable)"), (api, "Litestar GEA API\nservices.update_run"), (db, "PostgreSQL\ntable Run")], 1450)
    p.activation(api, 345, 800)
    p.message(form, wizard, 275, "change({ field: value }) on every edit")
    p.text(wizard + 18, 288, 330, 44, "collect the changed fields;\nwait for 800 ms without edits", 14, False, "left", GRAY)
    p.message(wizard, api, 380, "PATCH /runs/{id} · If-Match · the changed fields")
    p.message(api, db, 435, "BEGIN; SELECT the run FOR UPDATE")
    p.message(db, api, 485, "the run with its columns", True)
    p.frame(95, 530, 1010, 205, "alt [If-Match is not the ETag of the saved run]", AMBER)
    p.message(api, wizard, 595, "412 { data: null, error: precondition_failed }", True)
    p.message(wizard, api, 650, "GET /runs/{id}: the version saved elsewhere")
    p.message(wizard, form, 705, "saveState = conflict; the user decides", True)
    p.frame(95, 790, 1010, 170, "alt [a field that does not exist, or a value of the wrong type]", AMBER)
    p.message(api, wizard, 855, "422 validation_failed: one issue per field; nothing stored", True)
    p.message(wizard, form, 910, "saveError, fieldErrors(step)", True)
    p.message(api, db, 1020, "UPDATE Run SET only the columns that changed")
    p.text(db + 18, 1035, 190, 44, "trigger: only while the run\nis a draft; Revision + 1", 14, False, "left", GRAY)
    p.message(db, api, 1110, "stored; read the run again; COMMIT", True)
    p.message(api, wizard, 1165, "200: the run, what is still missing, its new ETag", True)
    p.message(wizard, form, 1220, "saveState = saved; run.issues: what is missing", True)
    p.message(form, wizard, 1300, "Next: save(fields)")
    p.message(wizard, api, 1355, "PATCH /runs/{id} at once, with what was still waiting")
    p.text(40, 1470, 1480, 28, "A half-filled run is saved and answered with 200 and ready = false: saving a draft never fails because the user is not finished.", 16, True)
    p.text(40, 1508, 1480, 28, "One save is in flight; later changes wait and go out as one PATCH. One ETag guards the whole run. Every field is a column of Run.", 16)
    return p


def gea_submit_delivery():
    p = Page("16 GEA submit and delivery", "GEA wizard · review, submit and delivery to Snowflake",
             "API and database implemented · the relay and the Snowflake side are the target; their functions exist in PostgreSQL.", 1900, 1775)
    wizard, api, db, relay, sf = 170, 560, 950, 1340, 1720
    p.participants([(wizard, "Vue wizard\nuseRunWizard"), (api, "Litestar GEA API"), (db, "PostgreSQL\nschema gea"), (relay, "Relay\n(not built yet)"), (sf, "Snowflake\nGEA.CONTROL")], 1645)
    p.activation(api, 395, 520)
    p.message(wizard, api, 275, "GET /runs/{id}/review")
    p.message(api, wizard, 330, "200: ready, missing fields per step, preview · ETag of the run", True)
    p.message(wizard, api, 410, "POST /runs/{id}/submit · Idempotency-Key · If-Match")
    p.message(api, db, 465, "BEGIN; receipt for (caller, key)? then replay it")
    p.message(api, db, 520, "SELECT run FOR UPDATE; compare the ETag")
    p.frame(95, 565, 950, 110, "alt [changed after the review, or a required field is empty]", AMBER)
    p.message(api, wizard, 640, "412 precondition_failed · 422 not_ready + issues", True)
    p.message(api, db, 740, "build document; canonical JSON; SHA-256; INSERT DataContract")
    p.text(db + 18, 752, 330, 64, "triggers: hash and document must equal\nthe saved columns; queue the delivery;\nrun moves to queued", 14, False, "left", GRAY)
    p.message(api, db, 860, "store the receipt and the audit event; COMMIT")
    p.message(api, wizard, 915, "201: the contract + Location; the run is locked", True)
    p.frame(830, 965, 1030, 555, "after the commit · independent of the request", GRAY, True)
    p.message(relay, db, 1040, "ClaimContractDeliveries(): contracts allowed to start")
    p.message(db, relay, 1095, "DocumentCanonical + ContentHash", True)
    p.message(relay, sf, 1150, "MERGE DATA_CONTRACT: same id, same hash")
    p.message(sf, relay, 1205, "stored; SHA2 matches", True)
    p.message(relay, sf, 1260, "EXECUTE TASK RUN_PIPELINE (contractId)")
    p.message(relay, db, 1315, "CompleteContractDelivery()")
    p.text(sf - 135, 1333, 270, 26, "task graph runs the steps (page 17)", 14, False, "center", GRAY)
    p.message(relay, sf, 1405, "read RUN_STATUS, RUN_STEP_STATUS, RUN_LOG")
    p.message(relay, db, 1465, "RecordExecutionStatus(), RecordExecutionLog()")
    p.message(wizard, api, 1575, "GET /runs/{id}/execution · If-None-Match (polling)")
    p.message(api, wizard, 1630, "304 unchanged · or 200: status of every step", True)
    p.text(40, 1685, 1820, 28, "A failed Snowflake write never undoes the contract: the delivery stays pending and is retried with the same document and hash.", 16, True)
    p.text(40, 1723, 1820, 28, "Repeating the submit with the same Idempotency-Key returns the same contract. A failed run is resolved (POST /runs/{id}/resolution) and resubmitted as version n + 1.", 16)
    return p


def gea_run_execution():
    p = Page("17 GEA run execution", "GEA run execution · who computes, who decides, who carries",
             "PostgreSQL and API implemented · the Snowflake pipeline is draft SQL that was never executed · the relay is not built.", 1900, 1500)
    # The user's side: three kinds of request.
    p.frame(40, 140, 1820, 150, "USER · Vue client → GEA API (Python: validates and records, never computes)")
    p.box(65, 180, 560, 85, "Start\nPOST /runs/{id}/submit · POST /jobs", size=17)
    p.box(670, 180, 560, 85, "Watch\nGET /runs/{id}/execution · /logs · GET /jobs/{id}", size=17)
    p.box(1275, 180, 560, 85, "Decide\nPOST /runs/{id}/cancel · /resolution", size=17)

    p.frame(40, 350, 600, 840, "POSTGRESQL · decides when, keeps state", GREEN)
    p.box(65, 395, 550, 130, "Run · Job · DataContract\nA run is submitted on its own or in a job\nJobOrdinal · MaxParallel\nCancelRequestedAt · Resolution", GREEN, size=16)
    p.box(65, 575, 550, 150, "ContractDelivery · ClaimContractDeliveries()\nA run on its own: at the next claim\nA job: in order, MaxParallel at a time\nMaxParallel 1 = one after another", GREEN, size=16)
    p.box(65, 775, 550, 130, "RunExecution · RunExecutionStep · RunLog\nThe copy of the status the API reads\nJobSummary: status and progress of a job", GREEN, size=16)
    p.box(65, 955, 550, 105, "FailStaleExecutions(30 minutes)\nAn execution that went silent is failed,\nit does not stay running", GREEN, size=16)
    p.box(65, 1100, 550, 65, "View RunCancelRequest: cancels to pass on", GREEN, size=16)
    p.line([(560, 290), (560, 350)], color=GREEN)

    p.frame(690, 350, 460, 840, "RELAY · Python, role gea_relay · NOT BUILT", GRAY, True)
    p.box(715, 395, 410, 105, "No computation, no waiting:\nevery call is short\nand can be repeated", GRAY, dashed=True, size=16)
    p.box(715, 575, 410, 150, "1 Deliver\nclaim → MERGE the contract\n→ read the hash back\n→ EXECUTE TASK", GRAY, dashed=True, size=16)
    p.box(715, 775, 410, 130, "2 Report\nstatus, steps and log lines\ncopied every few seconds", GRAY, dashed=True, size=16)
    p.box(715, 955, 410, 105, "3 Watch\nreport lost graph runs;\ncall FailStaleExecutions()", GRAY, dashed=True, size=16)
    p.box(715, 1100, 410, 65, "4 Cancel: pass the request on", GRAY, dashed=True, size=16)

    p.frame(1200, 350, 660, 840, "SNOWFLAKE · executes · draft SQL, never executed", AMBER)
    p.box(1225, 395, 610, 105, "GEA.CONTROL.DATA_CONTRACT\nThe frozen document, same id and SHA-256\nEvery step reads its configuration from it", AMBER, size=16)
    p.box(1225, 550, 610, 105, "Task graph GEA.CONTROL.RUN_PIPELINE\nOne graph run per contract · runs overlap\nOVERLAP_POLICY = ALLOW_ALL_OVERLAP", AMBER, size=16)
    steps = ["Data and\nset up", "Segmen-\ntation", "Actuals", "IBNR", "Assigning\nexpected", "Ultimate\ncalculation", "Actual /\nExpected"]
    for index, label in enumerate(steps):
        x = 1225 + index * 90
        p.box(x, 700, 70, 70, label, AMBER, size=12)
        if index:
            p.line([(x - 20, 735), (x, 735)], color=AMBER)
    p.text(1225, 778, 610, 44, "Each task calls RUN_STEP, which calls the step's procedure GEA.STEP.<STEP>:\nset-based SQL, or Snowpark Python where SQL cannot express it", 14, False, "center", GRAY)
    p.box(1225, 835, 610, 70, "Finalizer RUN_PIPELINE_END: the terminal status", AMBER, size=16)
    p.box(1225, 955, 610, 105, "RUN_STATUS · RUN_STEP_STATUS · RUN_LOG\nWritten before and after every step\nStatements tagged gea:<contractId>:<stepKey>", AMBER, size=16)
    p.box(1225, 1100, 610, 65, "RUN_STATUS.CANCEL_REQUESTED_AT: stop before the next step", AMBER, size=16)
    p.line([(1530, 500), (1530, 550)], color=AMBER)
    p.line([(1530, 655), (1530, 700)], color=AMBER)

    # What crosses the borders.
    p.line([(615, 650), (715, 650)], color=GRAY)
    p.line([(1125, 620), (1163, 620), (1163, 447), (1225, 447)], color=GRAY)
    p.line([(1125, 680), (1187, 680), (1187, 603), (1225, 603)], color=GRAY)
    p.line([(1225, 1008), (1175, 1008), (1175, 840), (1125, 840)], dashed=True, color=GREEN)
    p.line([(715, 840), (615, 840)], dashed=True, color=GREEN)
    p.line([(715, 1008), (615, 1008)], color=GRAY)
    p.line([(615, 1132), (715, 1132)], color=GRAY)
    p.line([(1125, 1132), (1225, 1132)], color=GRAY)

    p.frame(40, 1250, 1820, 150, "HOW RUNS ARE ORDERED · decided at submit, enforced in PostgreSQL")
    p.box(65, 1290, 425, 85, "One run\nstarts at the next claim", size=16)
    p.box(513, 1290, 425, 85, "Job, maxParallel omitted\nall its runs at once", size=16)
    p.box(961, 1290, 425, 85, "Job, maxParallel n\nin the order of runIds, n at a time", size=16)
    p.box(1409, 1290, 426, 85, "Job, maxParallel 1\nstrictly one after another", size=16)
    p.text(40, 1425, 1820, 28, "State is rows, written by the system that does the work and copied to the one the user talks to. Nothing in Python computes, waits for, or sequences a step.", 16, True)
    p.text(40, 1460, 1820, 28, "A cancelled run is a failed run with the reason. A failed run does not stop its job. To confirm on a real account: several graph runs of one task graph at the same time.", 16)
    return p


PAGES = [
    ("01-layer-overview", layers),
    ("02-data-contract", contract),
    ("03-silver-to-gold", regional),
    ("04-components", components),
    ("05-functions-and-bindings", functions),
    ("06-calculation-sequence", calculation),
    ("07-audit-sequence", audit),
    ("08-contract-storage-aws-orchestration", aws_orchestration),
    ("09-parameter-layer-sequence", parameter_sequence),
    ("10-assessment-worker", assessment_worker),
    ("11-assessment-client-updates", assessment_updates),
    ("12-calculation-retry-sequence", calculation_retry),
    ("13-gea-components", gea_components),
    ("14-gea-data-model", gea_data_model),
    ("15-gea-wizard-save", gea_wizard_save),
    ("16-gea-submit-delivery", gea_submit_delivery),
    ("17-gea-run-execution", gea_run_execution),
]


def main():
    root = ET.Element("mxfile", host="app.diagrams.net", modified="2026-09-17T00:00:00.000Z",
                      agent="Architecture documentation generator", version="24.7.17", type="device", compressed="false")
    for index, (name, build) in enumerate(PAGES, 1):
        page = build()
        (OUT / f"{name}.svg").write_text(page.svg(), encoding="utf-8")
        page.drawio(root, index)
    ET.indent(root)
    (OUT / DRAWIO).write_text(ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")
    print(f"Created {len(PAGES)} SVG diagrams and one editable {len(PAGES)}-page draw.io file.")


if __name__ == "__main__":
    main()
