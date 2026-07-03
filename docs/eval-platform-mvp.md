# Agent Evaluation Platform MVP

이 MVP는 Agent 실행과 평가를 분리한다. 테스트 케이스는 공통 레지스트리에 저장하고, Agent는 Adapter로 호출하며, 결과는 rule-based evaluator와 선택적 LLM-as-Judge 평가를 거쳐 버전별 리포트로 비교한다.

## Scope

- 테스트 케이스 CSV/DB 관리
- Agent API 호출
- 결과 저장
- Rule-based evaluator
- 선택적 LLM-as-Judge evaluator
- 리포트 API 생성

## Storage

SQLite DB 위치:

```text
eval_platform/eval_platform.db
```

주요 테이블:

- `eval_cases`: 테스트 케이스 레지스트리
- `eval_runs`: 평가 실행 메타데이터
- `eval_case_results`: 케이스별 실행/평가 결과

## CSV Format

```csv
case_id,title,input_json,expected_output_json,assertions_json,tags,category,source
login_terms,로그인 약관 확인,"{""prompt"":""약관 화면 확인""}","{""status"":""PASS""}","{""required_fields"":[""status""],""required_evidence"":[""이용약관""],""forbidden_claims"":[""결제 완료""],""min_groundedness"":0.7,""max_hallucination"":0.2}","login,smoke",auth,csv
```

`assertions_json` 지원 필드:

- `required_fields`: Agent output에 반드시 있어야 하는 dotted path 목록
- `required_evidence`: output 안에 근거로 포함되어야 하는 문자열 목록
- `forbidden_claims`: output 안에 나오면 hallucination으로 보는 문자열 목록
- `min_groundedness`: 최소 groundedness
- `max_hallucination`: 최대 hallucination 비율

## API

### Import Cases

```bash
curl -X POST http://localhost:8000/api/eval/cases/import-csv \
  -H 'Content-Type: application/json' \
  -d '{"csv_text":"case_id,title,input_json,expected_output_json,assertions_json,tags,category,source\nsample,Sample,\"{\"\"prompt\"\":\"\"run\"\"}\",\"{\"\"status\"\":\"\"PASS\"\"}\",\"{\"\"required_fields\"\":[\"\"status\"\"]}\",smoke,general,csv"}'
```

### Create Or Update A Case

```bash
curl -X POST http://localhost:8000/api/eval/cases \
  -H 'Content-Type: application/json' \
  -d '{
    "case_id": "sample",
    "title": "Sample",
    "input": {"prompt": "run"},
    "expected_output": {"status": "PASS"},
    "assertions": {"required_fields": ["status"]},
    "tags": ["smoke"],
    "category": "general"
  }'
```

### Run Evaluation

지원 Adapter:

- `http_json`: 외부 Agent API를 HTTP POST로 호출
- `auto_qa_local`: 현재 저장소의 `QAOrchestrator`를 직접 호출

`http_json` adapter는 `agent_url`로 아래 payload를 POST한다.

```json
{
  "case": {"case_id": "...", "title": "...", "input": {}},
  "input": {}
}
```

Agent 응답은 JSON object여야 한다. 권장 필드는 다음과 같다.

```json
{
  "status": "PASS",
  "success": true,
  "failure_reason": "",
  "cost_usd": 0.001,
  "evidence": ["..."]
}
```

실행:

```bash
curl -X POST http://localhost:8000/api/eval/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "prompt-v3 regression",
    "agent_name": "auto-qa",
    "agent_version": "prompt-v3",
    "adapter": "http_json",
    "agent_url": "http://localhost:9000/run",
    "case_ids": ["sample"],
    "use_llm_judge": false
  }'
```

`auto_qa_local` adapter는 케이스의 `input_json`에 auto-qa 실행 정보를 넣는다.

```json
{
  "title": "로그인 약관 확인",
  "package": "com.percent.aos.cooptd",
  "steps": [
    {"action": "launch_app", "target": "com.percent.aos.cooptd"},
    {"action": "verify", "target": "이용약관"}
  ]
}
```

### Report

```bash
curl http://localhost:8000/api/eval/runs/{run_id}/report
```

리포트 지표:

- `task_success_rate`
- `format_pass_rate`
- `groundedness`
- `hallucination_rate`
- `avg_latency_ms`
- `total_cost_usd`
- `regression_cases`
- `failure_reasons`

## Positioning

면접 설명은 다음처럼 가져가면 된다.

여러 팀에서 Agent를 만들기 시작하면서, 문제는 Agent를 만드는 것보다 품질을 일관되게 평가하고 버전별로 비교하는 것이었다. 그래서 Agent 실행과 평가를 분리한 Evaluation Platform을 설계했다. 각 Agent는 공통 Adapter로 호출하고, Test Case Registry에 저장된 케이스를 실행한 뒤, Rule-based validator와 LLM-as-Judge로 평가한다. 결과는 버전별로 비교해 prompt 변경이나 model 변경이 품질, latency, cost에 어떤 영향을 주는지 확인할 수 있게 했다.
