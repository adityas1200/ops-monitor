# Skill: Monitoring Agent

## Role
Continuously read operational telemetry across the data platform and produce a
normalized health summary of every pipeline/task.

## Sources
- **Snowflake**: `INFORMATION_SCHEMA.TASK_HISTORY`, task graph (root + predecessors),
  query history for long-running statements.
- **AWS Glue**: job runs (`get_job_runs`) — state, execution time, error string.
- **AWS Step Functions**: executions (`list_executions`) — status, duration.
- **CloudWatch Logs**: error/exception patterns in log groups.
- **DynamoDB**: stream/table operation logs and throttling events.

## Normalization
Map every source status to one of: `SUCCESS | FAILED | DELAYED | SKIPPED | RUNNING`.
- DELAYED = running longer than its rolling p90 duration OR past SLA.
- SKIPPED = upstream gate not met / condition false.

## Output Contract
```
{
  "generated_at": iso,
  "kpis": {"success","failed","delayed","skipped","running","total"},
  "pipelines": [{
     "id","name","platform","status","started_at","ended_at",
     "duration_s","sla_s","error","log_ref","upstream":[ids],"downstream":[ids]
  }]
}
```

## Procedure
1. Pull each source for the requested date range (default last 2 days).
2. Normalize and merge into a single pipeline list keyed by stable id.
3. Compute KPIs and flag delays vs SLA / historical p90.
4. Log every NEW failure/delay incident to incident-log memory.

## Memory Hooks
- Track per-pipeline historical durations to detect delays.
- Recognize previously-seen error signatures and tag them with prior RCA ids.
