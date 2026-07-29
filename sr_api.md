# v2 API 상세

## 1. 치트 목록 조회

```
GET /api/v2/cheats
```

```bash
curl "$BASE/api/v2/cheats"
```

응답의 `data.Cheats[]`에는 다음 정보가 들어갑니다.

- `Id`: 실행 시 사용할 고유 id
- `Category`, `DisplayName`, `Description`: 표시·분류 정보
- `Parameters[]`: 인자 정의
- `ScanErrors`: 등록 중 발생한 오류 목록

`Parameters[]`에는 `Name`, `Type`, `Required`, `DefaultValue`, `Min`, `Max`, `Step`, `Placeholder`, `EnumNames` 등이 포함됩니다. 클라이언트는 이 값을 사용해 실행 UI와 `Args` JSON을 구성합니다.

```json
{
  "success": true,
  "data": {
    "Cheats": [
      {
        "Id": "player.gold.add",
        "Parameters": [
          {
            "Name": "amount",
            "Type": "int",
            "Required": true,
            "DefaultValue": 1000,
            "Min": 1,
            "Max": 100000
          }
        ]
      }
    ],
    "ScanErrors": []
  }
}
```

특정 치트의 정의만 필요하면 다음 API를 사용합니다.

```
GET /api/v2/cheat?id=<id>
```

```bash
curl -G "$BASE/api/v2/cheat" --data-urlencode 'id=player.gold.add'
```

## 2. 치트 실행

```
POST /api/v2/cheats/execute
Content-Type: application/json

{"Id":"<id>","Args":{"<parameterName>":<value>}}
```

인자가 없는 치트:

```bash
curl -X POST "$BASE/api/v2/cheats/execute" \
  -H 'Content-Type: application/json' \
  -d '{"Id":"player.cache.clear","Args":{}}'
```

인자가 있는 치트:

```bash
curl -X POST "$BASE/api/v2/cheats/execute" \
  -H 'Content-Type: application/json' \
  -d '{"Id":"player.gold.add","Args":{"amount":1000}}'
```

- `Args`의 key는 치트 목록 `Parameters[].Name`과 일치해야 합니다. 대소문자는 구분하지 않습니다.
- 인자 검증 실패는 `success: false`, `errorCode: "invalid_args"`로 반환됩니다.
- 실행 결과는 `data.Result.Success`, `data.Result.Message`, `data.Result.Payload`, `data.Result.ErrorCode`에서 확인합니다.

## 3. 프로퍼티 정의 목록 조회

```
GET /api/v2/cheats/property
```

```bash
curl "$BASE/api/v2/cheats/property"
```

응답의 `data.Properties[]`에는 `Id`, `Category`, `DisplayName`, `Description`, `Type`, `CanRead`, `CanWrite`, `Min`, `Max`, `Step`, `EnumNames`, `IntervalMs` 등이 포함됩니다.

클라이언트는 `CanRead`가 `true`인 항목만 읽고, `CanWrite`가 `true`인 항목만 쓰기 UI/요청을 노출해야 합니다.

## 4. 프로퍼티 현재값 조회

전체 프로퍼티:

```
GET /api/v2/cheats/property/values
```

```bash
curl "$BASE/api/v2/cheats/property/values"
```

선택한 프로퍼티만 조회하려면 comma-separated ids query를 사용합니다.

```
GET /api/v2/cheats/property/values?ids=<id1>,<id2>
```

```bash
curl -G "$BASE/api/v2/cheats/property/values" \
  --data-urlencode 'ids=player.gold,player.god_mode'
```

`data.Properties[]`의 각 항목은 `Id`, `Value`, `Display`, `Error`를 가집니다. getter가 실패한 항목은 요청 전체가 실패하지 않고 해당 항목의 `Error`에 원인이 들어갈 수 있습니다.

## 5. 프로퍼티 값 쓰기

```
POST /api/v2/cheats/property/set
Content-Type: application/json

{"Id":"<id>","Value":<json-value>}
```

```bash
curl -X POST "$BASE/api/v2/cheats/property/set" \
  -H 'Content-Type: application/json' \
  -d '{"Id":"player.gold","Value":1000}'

curl -X POST "$BASE/api/v2/cheats/property/set" \
  -H 'Content-Type: application/json' \
  -d '{"Id":"player.god_mode","Value":true}'
```

- `Value`는 타입에 맞는 JSON 값으로 보냅니다. number는 JSON number, bool은 JSON boolean, 문자열/enum 이름은 JSON string입니다.
- 성공 시 `data.Value`와 `data.Display`에 적용 후 실제 값이 들어갑니다.
- 쓰기 불가 항목은 `errorCode: "read_only"`, 타입·범위 오류는 `errorCode: "invalid_value"`입니다.
