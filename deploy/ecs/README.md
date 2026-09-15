# ops-monitor — ECS / Fargate deployment

Single container: FastAPI serves `/api/*` and the built React UI from `frontend/dist`.

## Files

| File | Purpose |
|------|---------|
| `../../Dockerfile` | Multi-stage image (Node build → Python runtime) |
| `../../docker-compose.yml` | Local smoke test of the same image |
| `cloudformation.yml` | ECR (optional), ECS cluster/service, ALB, IAM, logs |
| `task-definition.json` | Standalone task def if you manage ECS outside CloudFormation |
| `secret.example.json` | Shape of the Anthropic secret in Secrets Manager |
| `deploy.ps1` / `deploy.sh` | Build + push to ECR (+ optional service roll) |

## 1. Local image check

```powershell
cd c:\Users\EKGAH\Documents\project\ops-monitor
docker compose up --build
# open http://localhost:8001  and  http://localhost:8001/api/health
```

Copy `.env.example` values into your shell or a local `.env` for LLM features.

## 2. Create the Anthropic secret (recommended)

```powershell
aws secretsmanager create-secret `
  --name ops-monitor/anthropic `
  --secret-string '{\"ANTHROPIC_API_KEY\":\"YOUR_KEY\",\"ANTHROPIC_BASE_URL\":\"https://chat.int.bayer.com/anthropic\",\"CLAUDE_MODEL\":\"claude-sonnet-4.5\"}'
```

## 3. Build & push to ECR

```powershell
$env:AWS_REGION = "us-east-1"   # your region
.\deploy\ecs\deploy.ps1
```

Note the printed image URI, e.g. `123456789012.dkr.ecr.us-east-1.amazonaws.com/ops-monitor:abc1234`.

## 4. Deploy the CloudFormation stack

You need an existing VPC plus public (ALB) and task subnets.

```powershell
aws cloudformation deploy `
  --template-file deploy\ecs\cloudformation.yml `
  --stack-name ops-monitor-prod `
  --capabilities CAPABILITY_NAMED_IAM `
  --parameter-overrides `
    ProjectName=ops-monitor `
    EnvironmentName=prod `
    VpcId=vpc-xxxxxxxx `
    "PublicSubnetIds=subnet-aaa,subnet-bbb" `
    "PrivateSubnetIds=subnet-ccc,subnet-ddd" `
    AssignPublicIp=ENABLED `
    ContainerImage=123456789012.dkr.ecr.us-east-1.amazonaws.com/ops-monitor:latest `
    AnthropicSecretArn=arn:aws:secretsmanager:us-east-1:123456789012:secret:ops-monitor/anthropic-XXXXXX `
    AnthropicBaseUrl=https://chat.int.bayer.com/anthropic `
    ClaudeModel=claude-sonnet-4.5 `
    CreateEcrRepository=false
```

Set `CreateEcrRepository=true` on first run if the repo does not exist yet, then pass the stack output `EcrRepositoryUri` as `ContainerImage` after pushing.

If tasks run in **public** subnets without NAT, keep `AssignPublicIp=ENABLED` and set `PrivateSubnetIds` to those same public subnet IDs.

## 5. Roll a new image

```powershell
$env:UPDATE_SERVICE = "true"
.\deploy\ecs\deploy.ps1
```

## Runtime notes

- **Health check:** `GET /api/health` (ALB + container).
- **Port:** `8001` (override with `PORT`).
- **Credentials:** Prefer Secrets Manager / task env over baking a `.env` into the image.
- **Persistence:** Agent memory and `connection_settings.json` are container-local. For durable storage across replacements, mount EFS on `/app/backend/app/data` and `/app/backend/app/memory/store`.
- **AWS live mode:** The task role allows read-only Glue / Step Functions / CloudWatch Logs / DynamoDB. Tighten `Resource` ARNs for production.
- **Snowflake:** Configure via the UI Settings page (stored under `app/data`) or inject monitoring defaults with env vars from `.env.example`.

## Standalone task definition

Edit placeholders in `task-definition.json` (`ACCOUNT_ID`, `REGION`, role ARNs, secret ARN), then:

```powershell
aws ecs register-task-definition --cli-input-json file://deploy/ecs/task-definition.json
aws ecs update-service --cluster ops-monitor-prod --service ops-monitor-prod --task-definition ops-monitor --force-new-deployment
```
