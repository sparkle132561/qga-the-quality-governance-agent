import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from quality.domain import Actor, Role
from quality.models import AuditEvent, Material
from quality.parsing import SUPPORTED, parse_material

MAX_BYTES = 12 * 1024 * 1024


def register_material_routes(app, settings, sessions, actor, visible_case):
    def output(material):
        return {
            "id": material.id,
            "filename": material.filename,
            "sha256": material.sha256,
            "status": material.status,
            "case_revision": material.case_revision,
            "parsed": material.parsed,
            "created_at": material.created_at.isoformat(),
        }

    @app.post("/api/cases/{case_id}/materials", status_code=201)
    def upload(case_id: str, who: Actor = Depends(actor), file: UploadFile = File()):
        if who.role == Role.ADMIN:
            raise HTTPException(403, "管理员不能代交商家资料")
        filename = Path(file.filename or "material").name[:200]
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED:
            raise HTTPException(415, "支持PDF、HTML、XLSX、CSV、PNG和JPEG")
        # 先查权限，避免为越权请求运行解析／OCR。
        with sessions() as session:
            visible_case(session, case_id, who)
        data = file.file.read(MAX_BYTES + 1)
        if not data or len(data) > MAX_BYTES:
            raise HTTPException(413, "文件为空或超过12MB")
        try:
            parsed = parse_material(data, suffix)
        except Exception:
            parsed = {
                "status": "failed",
                "units": [],
                "fields": [],
                "parser_version": "1",
                "engine": "unavailable",
                "warning": "解析失败，请检查文件格式、大小或OCR运行环境",
            }
        material_id = str(uuid4())
        stored_name = material_id + suffix
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        path = settings.upload_dir / stored_name
        path.write_bytes(data)
        try:
            with sessions.begin() as session:
                found = visible_case(session, case_id, who, lock=True)
                if found.status not in ("draft", "needs_info"):
                    raise HTTPException(409, "当前案件不能修改材料")
                found.revision += 1
                found.latest_analysis = None
                material = Material(
                    id=material_id,
                    case_id=case_id,
                    filename=filename,
                    stored_name=stored_name,
                    sha256=hashlib.sha256(data).hexdigest(),
                    media_type=file.content_type or "application/octet-stream",
                    status=parsed["status"],
                    case_revision=found.revision,
                    parsed=parsed,
                )
                session.add(material)
                session.flush()
                session.add(
                    AuditEvent(
                        case_id=case_id,
                        actor_id=who.user_id,
                        action="upload_material",
                        details={
                            "material_id": material_id,
                            "status": material.status,
                            "revision": found.revision,
                        },
                    )
                )
                result = output(material)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return result

    @app.get("/api/cases/{case_id}/materials")
    def list_materials(case_id: str, who: Actor = Depends(actor)):
        with sessions() as session:
            visible_case(session, case_id, who)
            return [
                output(material)
                for material in session.scalars(
                    select(Material)
                    .where(Material.case_id == case_id)
                    .order_by(Material.created_at)
                )
            ]

    @app.get("/api/cases/{case_id}/materials/{material_id}/download")
    def download(case_id: str, material_id: str, who: Actor = Depends(actor)):
        with sessions() as session:
            visible_case(session, case_id, who)
            material = session.scalar(
                select(Material).where(Material.id == material_id, Material.case_id == case_id)
            )
            if material is None:
                raise HTTPException(404, "材料不存在")
            path = settings.upload_dir / material.stored_name
            if not path.is_file():
                raise HTTPException(404, "原始文件不可用")
            # 强制附件与通用类型，不在本站执行上传HTML。
            return FileResponse(
                path, filename=material.filename, media_type="application/octet-stream"
            )
