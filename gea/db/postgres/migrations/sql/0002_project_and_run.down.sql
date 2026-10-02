-- 0002 Project and Run, downgrade.
DROP TABLE gea."ParameterOption";
DROP FUNCTION gea."RunConfiguration"(uuid);
DROP FUNCTION gea."RunSteps"(uuid);
DROP TABLE gea."RunStudyPeriodExclusion";
DROP TABLE gea."Run";
DROP TABLE gea."ProjectBenefit";
DROP TABLE gea."Project";
DROP FUNCTION gea."RunStudyPeriodExclusion_BeforeWrite"();
DROP FUNCTION gea."Run_BeforeDelete"();
DROP FUNCTION gea."Run_BeforeUpdate"();
DROP FUNCTION gea."Run_BeforeInsert"();
DROP FUNCTION gea."Project_RequiresBenefit"();
DROP FUNCTION gea."Project_BeforeUpdate"();
