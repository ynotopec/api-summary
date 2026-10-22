"""
title: Résumer les pièces jointes via MCP
author: Local
version: 1.2.0
description: Transmet les pièces jointes par blocs MCP, puis lance un résumé asynchrone avec suivi par identifiant. Aucun endpoint d’upload séparé.
"""
import asyncio
import base64
import json
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field


class BridgeError(ValueError):
    pass


class Tools:
    class Valves(BaseModel):
        OPENWEBUI_URL: str = Field(default="http://127.0.0.1:8080", description="URL backend de cette instance, accessible depuis son conteneur")
        MCP_SERVER_ID: str = Field(default="", description="ID info.id de la connexion MCP existante dans OpenWebUI, configurée en OAuth ou OAuth 2.1")
        BEGIN_TOOL: str = "upload_begin"
        CHUNK_TOOL: str = "upload_chunk"
        FINISH_TOOL: str = "upload_finish"
        START_TOOL: str = "start_summary"
        STATUS_TOOL: str = "get_status"
        RESULT_TOOL: str = "get_result"
        CONTROL_TIMEOUT_SECONDS: int = Field(default=60, ge=1)
        CHUNK_KIB: int = Field(default=256, ge=16, le=1024)
        MAX_FILES: int = Field(default=20, ge=1)
        MAX_FILE_MIB: int = Field(default=2048, ge=1)
        MAX_TOTAL_MIB: int = Field(default=4096, ge=1)
        TIMEOUT_SECONDS: int = Field(default=1800, ge=1)
        MAX_SUMMARY_CHARS: int = Field(default=40000, ge=100)
        TEMP_DIR: str = Field(default="", description="Répertoire temporaire sur disque ; vide = défaut système")

    def __init__(self):
        self.valves = self.Valves()

    def _attachments(self, entries):
        files = []
        seen = set()
        for item in entries or []:
            if not isinstance(item, dict) or item.get("type", "file") != "file":
                continue
            nested = item.get("file") or {}
            file_id = item.get("id") or nested.get("id")
            if not file_id:
                continue
            # Ignore attachment URLs: only retrieve originals from this instance.
            file_id = str(uuid.UUID(str(file_id)))
            if file_id in seen:
                continue
            seen.add(file_id)
            name = item.get("name") or nested.get("filename") or "document"
            name = str(name).replace("\\", "/").split("/")[-1]
            name = "".join(c for c in name if c.isprintable())[:200] or "document"
            files.append((file_id, name))
        if not files:
            raise BridgeError("Joignez au moins un fichier dans ce message.")
        if len(files) > self.valves.MAX_FILES:
            raise BridgeError("Nombre maximal de fichiers dépassé.")
        return files

    def _url(self, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
            raise BridgeError("URL de service invalide dans les Valves.")
        return value

    def _error_message(self, exc):
        if isinstance(exc, BridgeError):
            return str(exc)
        for child in getattr(exc, "exceptions", ()):
            message = self._error_message(child)
            if message:
                return message
        return None

    def _local_client(self):
        return httpx.AsyncClient(
            timeout=httpx.Timeout(30, read=self.valves.TIMEOUT_SECONDS, write=self.valves.TIMEOUT_SECONDS),
            follow_redirects=False,
        )

    @asynccontextmanager
    async def _session(self, request, user_data, metadata=None, event_call=None):
        # Native OpenWebUI connector owns OAuth grants, refresh and access checks.
        # These are internal APIs: validate on the deployed OpenWebUI version.
        from open_webui.models.config import Config
        from open_webui.models.users import Users
        from open_webui.utils.tools import connect_mcp_server

        server_id = self.valves.MCP_SERVER_ID.strip()
        if not server_id:
            raise BridgeError("Renseignez MCP_SERVER_ID avec l'ID de la connexion MCP OAuth.")
        if not isinstance(user_data, dict) or not user_data.get("id"):
            raise BridgeError("Identité utilisateur OpenWebUI absente.")
        user = await Users.get_user_by_id(user_data["id"])
        if user is None:
            raise BridgeError("Utilisateur OpenWebUI introuvable.")
        connections = await Config.get("tool_server.connections", [])
        connection = next((c for c in connections if c.get("type") == "mcp"
            and (c.get("info") or {}).get("id") == server_id), None)
        if not connection or connection.get("auth_type") not in {
            "system_oauth", "oauth_2.1", "oauth_2.1_static"
        }:
            raise BridgeError("La connexion MCP doit utiliser OAuth, OAuth 2.1 ou OAuth 2.1 (Static).")
        resolved = await connect_mcp_server(request, server_id, user, {
            "__metadata__": metadata or {}, "__event_call__": event_call,
        })
        if resolved is None:
            raise BridgeError("Connexion MCP désactivée, absente ou non autorisée.")
        client, specs = resolved
        try:
            available = {spec["name"] for spec in specs}
            required = {self.valves.BEGIN_TOOL, self.valves.CHUNK_TOOL,
                self.valves.FINISH_TOOL, self.valves.START_TOOL,
                self.valves.STATUS_TOOL, self.valves.RESULT_TOOL}
            if not required.issubset(available):
                raise BridgeError("Les six outils de transfert/résumé asynchrone doivent être disponibles et autorisés sur ce MCP.")
            if client.session is None:
                raise BridgeError("Session MCP non connectée.")
            yield client.session
        finally:
            # Same task as connect: required by the MCP SDK's task-group lifecycle.
            await client.disconnect()

    async def _call(self, session, name, arguments):
        result = await session.call_tool(name, arguments=arguments)
        if result.isError:
            raise BridgeError(f"Le serveur MCP a signalé une erreur pour {name}.")
        return result

    def _json_object(self, result):
        data = getattr(result, "structuredContent", None)
        if data is None:
            texts = [p.text for p in result.content if getattr(p, "type", None) == "text"]
            try:
                data = json.loads("\n".join(texts))
            except (TypeError, ValueError):
                raise BridgeError("Le MCP doit retourner un objet JSON valide.") from None
        if not isinstance(data, dict):
            raise BridgeError("Le MCP doit retourner un objet JSON.")
        return data

    def _json_result(self, result, key):
        value = self._json_object(result).get(key)
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise BridgeError(f"Réponse MCP : {key} absent ou invalide.")
        return value

    def _job_id(self, job_id):
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 1024:
            raise BridgeError("Identifiant de tâche absent ou invalide.")
        return job_id.strip()

    async def _transfer(self, session, path, name):
        result = await self._call(session, self.valves.BEGIN_TOOL, {
            "filename": name, "size_bytes": path.stat().st_size,
        })
        upload_id = self._json_result(result, "upload_id")
        with path.open("rb") as handle:
            index = 0
            while chunk := await asyncio.to_thread(handle.read, self.valves.CHUNK_KIB * 1024):
                await self._call(session, self.valves.CHUNK_TOOL, {
                    "upload_id": upload_id, "index": index,
                    "data_base64": base64.b64encode(chunk).decode("ascii"),
                })
                index += 1
        result = await self._call(session, self.valves.FINISH_TOOL, {"upload_id": upload_id})
        return self._json_result(result, "file_id")

    def _summary_text(self, result):
        texts = [part.text for part in result.content if getattr(part, "type", None) == "text"]
        if not texts and getattr(result, "structuredContent", None):
            texts = [json.dumps(result.structuredContent, ensure_ascii=False)]
        if not texts:
            raise BridgeError("MCP n'a retourné aucun résumé textuel.")
        text = "\n\n".join(texts)
        if len(text) > self.valves.MAX_SUMMARY_CHARS:
            text = text[:self.valves.MAX_SUMMARY_CHARS] + "\n[Résumé tronqué par la passerelle.]"
        return text

    async def summarize_attachments(
        self,
        instructions: str = "Résume en français les documents joints et cite leurs noms.",
        __files__: list = None,
        __request__=None,
        __event_emitter__=None,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_call__=None,
    ) -> str:
        """Transfère les originaux (documents ou ZIP) et lance un résumé en arrière-plan. Retourne job_id, pas le résumé. Ne pas relancer pour consulter une tâche existante ; utiliser get_summary_status/get_summary_result.
        :param instructions: Consignes de résumé ; ne pas fournir le contenu ni le base64 des fichiers.
        """
        async def status(message, done=False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": message, "done": done}})

        try:
            files = self._attachments(__files__)
            for url in (self.valves.OPENWEBUI_URL,):
                self._url(url)
            if __request__ is None:
                raise BridgeError("Requête OpenWebUI absente.")
            # Reuse this user's request authentication only against this OpenWebUI.
            auth_headers = {}
            for key in ("authorization", "cookie"):
                if __request__.headers.get(key):
                    auth_headers[key] = __request__.headers[key]
            if not auth_headers:
                raise BridgeError("Authentification utilisateur absente ; aucun jeton administrateur de secours.")
            total = 0
            remote_ids = []
            async with asyncio.timeout(self.valves.TIMEOUT_SECONDS):
                async with self._local_client() as local:
                    # MCP authentication is resolved for this user by OpenWebUI.
                    async with self._session(__request__, __user__, __metadata__, __event_call__) as session:
                        for index, (file_id, name) in enumerate(files, 1):
                            await status(f"Transfert du fichier {index}/{len(files)} : {name}")
                            with tempfile.TemporaryDirectory(dir=self.valves.TEMP_DIR or None) as directory:
                                path = Path(directory) / "original"
                                url = f"{self.valves.OPENWEBUI_URL.rstrip('/')}/api/v1/files/{quote(file_id, safe='')}/content"
                                size = 0
                                async with local.stream("GET", url, headers=auth_headers) as response:
                                    response.raise_for_status()
                                    with path.open("wb") as output:
                                        async for chunk in response.aiter_bytes(1024 * 1024):
                                            size += len(chunk)
                                            total += len(chunk)
                                            if size > self.valves.MAX_FILE_MIB * 1024**2 or total > self.valves.MAX_TOTAL_MIB * 1024**2:
                                                raise BridgeError("Limite de taille des fichiers dépassée.")
                                            await asyncio.to_thread(output.write, chunk)
                                remote_ids.append(await self._transfer(session, path, name))
                        await status("Fichiers transférés ; soumission de la tâche…")
                        async with asyncio.timeout(self.valves.CONTROL_TIMEOUT_SECONDS):
                            result = await self._call(session, self.valves.START_TOOL, {
                                "file_ids": remote_ids, "instructions": instructions,
                            })
                            job_id = self._json_result(result, "job_id")
            await status("Tâche acceptée ; vous pouvez revenir consulter le résultat.", True)
            return json.dumps({"job_id": job_id, "status": "submitted",
                "next": "Conserver job_id. Utiliser get_summary_status, puis get_summary_result quand completed. Ne pas relancer summarize_attachments ; aucune interrogation automatique."}, ensure_ascii=False)
        except BridgeError as exc:
            await status("Traitement interrompu.", True)
            return f"Erreur : {exc}"
        except Exception as exc:
            message = self._error_message(exc)
            if message:
                await status("Traitement interrompu.", True)
                return f"Erreur : {message}"
            # Do not expose URLs containing secrets, response bodies or auth headers.
            await status("Échec du transfert ou du résumé.", True)
            return "Erreur de connexion, d'authentification, de délai ou de serveur. Vérifiez la connexion MCP, autorisez votre compte dans les intégrations du chat, puis réessayez. Consultez les journaux serveur si nécessaire. Des fichiers déjà transférés peuvent rester sur le service ; appliquez sa politique de rétention."

    async def _read_job(self, job_id, want_result, request, user, metadata, event_call):
        try:
            job_id = self._job_id(job_id)
            if request is None:
                raise BridgeError("Requête OpenWebUI absente.")
            async with asyncio.timeout(self.valves.CONTROL_TIMEOUT_SECONDS):
                # A new OAuth-authenticated session on every later consultation.
                async with self._session(request, user, metadata, event_call) as session:
                    result = await self._call(session, self.valves.STATUS_TOOL, {"job_id": job_id})
                    data = self._json_object(result)
                    state = data.get("status")
                    if state not in {"pending", "running", "completed", "failed", "cancelled"}:
                        raise BridgeError("Statut MCP invalide (pending/running/completed/failed/cancelled attendu).")
                    if data.get("job_id", job_id) != job_id:
                        raise BridgeError("Le MCP a retourné un autre identifiant de tâche.")
                    if want_result and state == "completed":
                        result = await self._call(session, self.valves.RESULT_TOOL, {"job_id": job_id})
                        return json.dumps({"job_id": job_id, "status": state,
                            "summary": self._summary_text(result)}, ensure_ascii=False)
                    return json.dumps({"job_id": job_id, "status": state,
                        "next": "Utiliser get_summary_result pour lire le résumé." if state == "completed"
                            else "Reconsulter plus tard." if state in {"pending", "running"}
                            else "Traitement terminé sans résumé. Consulter le service pour le diagnostic."}, ensure_ascii=False)
        except Exception as exc:
            message = self._error_message(exc)
            return json.dumps({"error": message or "Consultation impossible : vérifiez vos droits, l'autorisation OAuth, l'expiration de la tâche et la connexion MCP.", "job_id": job_id if isinstance(job_id, str) and len(job_id) <= 1024 else None}, ensure_ascii=False)

    async def get_summary_status(
        self, job_id: str, __request__=None, __user__: dict = None,
        __metadata__: dict = None, __event_call__=None,
    ) -> str:
        """Consulte une fois l'état d'un résumé déjà lancé. Aucun transfert ni attente active. Ne pas interroger en boucle.
        :param job_id: Identifiant retourné par summarize_attachments.
        """
        return await self._read_job(job_id, False, __request__, __user__, __metadata__, __event_call__)

    async def get_summary_result(
        self, job_id: str, __request__=None, __user__: dict = None,
        __metadata__: dict = None, __event_call__=None,
    ) -> str:
        """Récupère le résumé si la tâche est completed ; sinon retourne son état sans attendre. Pas de nouvel upload.
        :param job_id: Identifiant retourné par summarize_attachments.
        """
        return await self._read_job(job_id, True, __request__, __user__, __metadata__, __event_call__)
