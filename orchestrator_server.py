"""qa-auto Orchestrator — Linux 서버에서 실행되는 중앙 관리 서버"""
import logging
import os
import time
import yaml
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx

from config import Config
from planner_node import PlannerNode

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
cfg = Config()

_pipelines_dir = cfg.paths.project_root / "pipelines"
_pipelines_dir.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="qa-auto Orchestrator", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ─────────────────────────────────────────────
# 에이전트 레지스트리
# ─────────────────────────────────────────────
_agents: dict[str, dict] = {}  # name → {ip, port, last_seen}

AGENT_TIMEOUT_SEC = 90  # 이 시간 동안 heartbeat 없으면 오프라인으로 표시

def _agent_url(name: str) -> str:
    agent = _agents.get(name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"에이전트 없음: {name}")
    return f"http://{agent['ip']}:{agent['port']}"

def _is_online(agent: dict) -> bool:
    return time.time() - agent.get("last_seen", 0) < AGENT_TIMEOUT_SEC


# ─────────────────────────────────────────────
# Pydantic 모델
# ─────────────────────────────────────────────
class AgentRegisterRequest(BaseModel):
    name: str
    ip: str
    port: int = 8000

class GeneratePlanRequest(BaseModel):
    scenario: str
    package: str = ""

class SaveTemplateRequest(BaseModel):
    name: str
    content: str
    scenario: str = ""

class SavePipelineRequest(BaseModel):
    name: str
    steps: list[dict] = []
    nodes: list[dict] = []
    edges: list[dict] = []


# ─────────────────────────────────────────────
# 에이전트 관리 API
# ─────────────────────────────────────────────
@app.post("/api/agents/register")
def register_agent(req: AgentRegisterRequest):
    _agents[req.name] = {"ip": req.ip, "port": req.port, "last_seen": time.time()}
    logger.info(f"[Orchestrator] 에이전트 등록: {req.name} ({req.ip}:{req.port})")
    return {"success": True}

@app.post("/api/agents/heartbeat")
def heartbeat(req: AgentRegisterRequest):
    if req.name in _agents:
        _agents[req.name]["last_seen"] = time.time()
    else:
        _agents[req.name] = {"ip": req.ip, "port": req.port, "last_seen": time.time()}
    return {"success": True}

@app.get("/api/agents")
def list_agents():
    return {
        "agents": [
            {"name": name, "ip": a["ip"], "port": a["port"], "online": _is_online(a)}
            for name, a in _agents.items()
        ]
    }


# ─────────────────────────────────────────────
# 에이전트 프록시 API
# ─────────────────────────────────────────────
async def _proxy_get(agent_name: str, path: str):
    url = _agent_url(agent_name) + path
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        return r.json()

async def _proxy_post(agent_name: str, path: str, body: dict):
    url = _agent_url(agent_name) + path
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json=body)
        return r.json()

@app.get("/api/agents/{agent_name}/device")
async def proxy_get_device(agent_name: str):
    return await _proxy_get(agent_name, "/api/device")

@app.get("/api/agents/{agent_name}/devices")
async def proxy_list_devices(agent_name: str):
    return await _proxy_get(agent_name, "/api/devices")

@app.get("/api/agents/{agent_name}/preflight")
async def proxy_preflight(agent_name: str):
    return await _proxy_get(agent_name, "/api/preflight")

@app.get("/api/agents/{agent_name}/screen/snapshot")
async def proxy_snapshot(agent_name: str):
    url = _agent_url(agent_name) + "/api/screen/snapshot"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url)
    from fastapi.responses import Response
    return Response(content=r.content, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

@app.post("/api/agents/{agent_name}/test/run")
async def proxy_run_test(agent_name: str, body: dict):
    return await _proxy_post(agent_name, "/api/test/run", body)

@app.post("/api/agents/{agent_name}/test/stop")
async def proxy_stop_test(agent_name: str, body: dict):
    return await _proxy_post(agent_name, "/api/test/stop", body)

@app.post("/api/agents/{agent_name}/pipeline/run")
async def proxy_run_pipeline(agent_name: str, body: dict):
    return await _proxy_post(agent_name, "/api/pipeline/run", body)

@app.get("/api/agents/{agent_name}/packages")
async def proxy_packages(agent_name: str):
    return await _proxy_get(agent_name, "/api/packages")

@app.get("/api/agents/{agent_name}/apks")
async def proxy_apks(agent_name: str):
    return await _proxy_get(agent_name, "/api/apks")


# ─────────────────────────────────────────────
# 템플릿 API
# ─────────────────────────────────────────────
@app.get("/api/templates")
def list_templates():
    try:
        files = sorted(cfg.paths.templates_dir.glob("*.yaml"))
        return {"templates": [f.stem for f in files]}
    except Exception as e:
        return {"templates": [], "error": str(e)}

@app.get("/api/templates/{name}")
def get_template(name: str):
    path = cfg.paths.templates_dir / f"{name}.yaml"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Template not found: {name}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {"template": data}

@app.post("/api/templates")
def save_template(req: SaveTemplateRequest):
    try:
        data = yaml.safe_load(req.content)
        if req.scenario:
            data["source_scenario"] = req.scenario
        data.setdefault("preconditions", [])
        safe_name = req.name.replace(" ", "_")
        out_path = cfg.paths.templates_dir / f"{safe_name}.yaml"
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False)
        return {"success": True, "filename": out_path.name}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.delete("/api/templates/{name}")
def delete_template(name: str):
    path = cfg.paths.templates_dir / f"{name}.yaml"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Template not found: {name}")
    path.unlink()
    return {"success": True}


# ─────────────────────────────────────────────
# 파이프라인 API
# ─────────────────────────────────────────────
@app.get("/api/pipelines")
def list_pipelines():
    files = sorted(_pipelines_dir.glob("*.json"))
    return {"pipelines": [f.stem for f in files]}

@app.get("/api/pipelines/{name}")
def get_pipeline(name: str):
    import json as _json
    path = _pipelines_dir / f"{name}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Pipeline not found: {name}")
    with open(path, encoding="utf-8") as f:
        return _json.load(f)

@app.post("/api/pipelines")
def save_pipeline(req: SavePipelineRequest):
    import json as _json
    safe_name = req.name.strip().replace(" ", "_")
    if not safe_name:
        raise HTTPException(status_code=400, detail="이름을 입력해주세요.")
    path = _pipelines_dir / f"{safe_name}.json"
    with open(path, "w", encoding="utf-8") as f:
        _json.dump({"name": req.name, "steps": req.steps, "nodes": req.nodes, "edges": req.edges}, f, ensure_ascii=False, indent=2)
    return {"success": True, "filename": path.name}

@app.delete("/api/pipelines/{name}")
def delete_pipeline(name: str):
    path = _pipelines_dir / f"{name}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Pipeline not found: {name}")
    path.unlink()
    return {"success": True}


# ─────────────────────────────────────────────
# LLM 플랜 생성 API
# ─────────────────────────────────────────────
@app.post("/api/plan/generate")
def generate_plan(req: GeneratePlanRequest):
    if not req.scenario.strip():
        raise HTTPException(status_code=400, detail="시나리오를 입력해주세요.")
    try:
        planner = PlannerNode(model=cfg.gemini.model)
        plan = planner.create_test_plan(req.scenario.strip(), req.package.strip())
        yaml_data = {
            "title": plan.title, "description": plan.description,
            "package": plan.package, "steps": [s.model_dump() for s in plan.steps],
            "expected_results": plan.expected_results,
        }
        yaml_str = yaml.dump(yaml_data, allow_unicode=True, sort_keys=False)
        return {"title": plan.title, "steps_count": len(plan.steps), "yaml": yaml_str}
    except Exception as e:
        logger.exception("generate_plan failed")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# 헬스체크
# ─────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "agents_online": sum(1 for a in _agents.values() if _is_online(a))}


# ─────────────────────────────────────────────
# React SPA 서빙
# ─────────────────────────────────────────────
_dist = Path(__file__).parent / "frontend" / "dist"

if _dist.exists():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/")
    async def serve_root():
        return FileResponse(str(_dist / "index.html"))

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # API 경로는 위에서 처리됨
        candidate = _dist / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_dist / "index.html"))
else:
    logger.warning(f"Frontend dist not found at: {_dist} — UI 없이 API만 실행됩니다.")


# ─────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("ORCHESTRATOR_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
