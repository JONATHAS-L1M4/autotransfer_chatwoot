# -*- coding: utf-8 -*-
"""
Chatwoot - Balanceador (Time -> Agente menos carregado) + Retorno simples do status da conversa

Fluxo principal (auto_assign_team_then_agent):
  0) Se a conversa já tiver assignee -> NÃO faz nada, retorna status simplificado.
  1) Atribui a conversa à EQUIPE (sem agente)
  2) Define a PRIORIDADE
  3) Se a prioridade estiver configurada para "auto_assign": escolhe o agente menos carregado do time e atribui
  4) Envia MENSAGEM PRIVADA no template solicitado
  5) Retorna o STATUS SIMPLIFICADO da conversa (conversation_id, status, priority, assignee{id,name}, team{id,name})

Função extra:
  - get_conversation_simple(conversation_id): retorna o mesmo STATUS SIMPLIFICADO.

Observações:
- "Ativas" por padrão = ("open", "pending"), ajustável via config.
- Em produção, usar TLS válido (verify_tls=True + CA) via config.
- Config externo controla as prioridades que disparam auto-assign.

Arquivo de config (YAML apenas; ver detalhes abaixo):
  auto_assign_by_priority: ["urgent"]
  statuses_for_load: ["open", "pending"]
  verify_tls: false
"""

import os
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from dotenv import load_dotenv
load_dotenv()

# ========================
# Configuração via .env e arquivo externo (YAML)
# ========================

API_ACCESS_TOKEN = os.getenv("CHATWOOT_TOKEN")
ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID")
DOMAIN = (os.getenv("CHATWOOT_DOMAIN") or "").rstrip("/")
TIMEOUT: int = int(os.getenv("CHATWOOT_TIMEOUT", "15"))

CONFIG_PATH = os.getenv("CHATWOOT_CONFIG_PATH") or "chatwoot_config.yaml"

def _load_config() -> Dict[str, Any]:
    """
    Carrega config a partir de YAML (somente).
    Chaves suportadas:
      - auto_assign_by_priority: List[str]
      - statuses_for_load: List[str]
      - verify_tls: bool
    """
    base = {
        "auto_assign_by_priority": ["urgent"],
        "statuses_for_load": ["open", "pending"],
        "verify_tls": False,
    }
    if not os.path.exists(CONFIG_PATH):
        return base

    data: Dict[str, Any] = {}
    try:
        import yaml  # type: ignore
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        # Se qualquer erro de leitura/parse, volta pro default
        data = {}

    # Só aplica chaves reconhecidas
    for k in ("auto_assign_by_priority", "statuses_for_load", "verify_tls"):
        if k in data:
            base[k] = data[k]
    return base

CONFIG = _load_config()
AUTO_ASSIGN_PRIOS = {str(x).lower() for x in CONFIG.get("auto_assign_by_priority", ["urgent"])}
STATUSES_FOR_LOAD: Tuple[str, ...] = tuple(CONFIG.get("statuses_for_load", ["open", "pending"]))
VERIFY_TLS: bool = bool(CONFIG.get("verify_tls", False))

# ========================
# Sessão HTTP única
# ========================
session = requests.Session()
session.verify = VERIFY_TLS  # Em produção: True + CA (controlado via config)
session.headers.update({
    "api_access_token": API_ACCESS_TOKEN or "",
    "Content-Type": "application/json",
})

# ========================
# Helpers HTTP / JSON
# ========================
def _json_or_text(resp: requests.Response) -> Dict[str, Any]:
    """Tenta retornar JSON; se não der, devolve status/text."""
    try:
        return resp.json()
    except ValueError:
        return {"status_code": resp.status_code, "text": resp.text}

def _get_json(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = TIMEOUT
) -> Optional[Any]:
    """GET que já retorna JSON ou None em erro."""
    try:
        r = session.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError):
        return None

def _post_json(
    url: str,
    payload: Dict[str, Any],
    *,
    timeout: int = TIMEOUT
) -> Tuple[int, Dict[str, Any]]:
    """POST que retorna (status_code, json_ou_texto) e lida com erros de rede."""
    try:
        r = session.post(url, json=payload, timeout=timeout)
        return r.status_code, _json_or_text(r)
    except requests.RequestException as e:
        return 0, {"error": "network", "detail": str(e)}

def _extract_list_like(
    payload: Any,
    *,
    prefer_keys: Iterable[str] = ("data","results","items","payload","conversations","list","records")
) -> List[Any]:
    """Extrai a primeira lista encontrada em chaves comuns; se vier um objeto único, retorna [obj]."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in prefer_keys:
            v = payload.get(k)
            if isinstance(v, list):
                return v
        return [payload]
    return []

# ========================
# Domínio: Teams / Members
# ========================
def get_team_members(team_id: Union[str, int], *, timeout: int = TIMEOUT) -> List[Dict[str, str]]:
    """
    Retorna [{id, name}] para os membros de um time, com tolerância a variações de payload.
    Em erro, retorna [].
    """
    url = f"{DOMAIN}/api/v1/accounts/{ACCOUNT_ID}/teams/{team_id}/team_members"
    payload = _get_json(url, timeout=timeout)
    if payload is None:
        return []

    members: Any = payload
    if isinstance(payload, dict):
        for key in ("team_members", "members", "data", "results", "items"):
            if isinstance(payload.get(key), list):
                members = payload[key]
                break
        else:
            members = [payload]

    def pick_id_name(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
        if not isinstance(item, dict):
            return None

        if "id" in item and ("name" in item or "full_name" in item):
            return {"id": str(item["id"]), "name": item.get("name") or item.get("full_name")}

        for id_key, name_keys in (
            ("user_id", ("user_name", "name", "full_name")),
            ("member_id", ("member_name", "name", "full_name")),
        ):
            if id_key in item:
                for nk in name_keys:
                    if item.get(nk):
                        return {"id": str(item[id_key]), "name": item[nk]}

        user = item.get("user")
        if isinstance(user, dict):
            uid = user.get("id") or user.get("user_id")
            uname = user.get("name") or user.get("full_name") or user.get("email")
            if uid and uname:
                return {"id": str(uid), "name": uname}

        return None

    if not isinstance(members, list):
        return []

    return [p for p in (pick_id_name(m) for m in members) if p]

# ========================
# Domínio: Conversations
# ========================

PRIORITY_MAP: Dict[Union[int, str], str] = {
    0: "low", 1: "medium", 2: "high", 3: "urgent",
    "low": "low", "medium": "medium", "high": "high", "urgent": "urgent",
}

def _iter_conversations(
    *,
    team_id: Optional[Union[str,int]] = None,
    statuses: Tuple[str, ...] = ("open", "pending"),
    per_page: int = 50,
    max_pages: int = 200
) -> Iterable[Dict[str, Any]]:
    """
    Itera conversas por páginas e filtra por status e, se possível, por team_id.
    Se o filtro team_id não for aceito pelo endpoint, filtra em memória.
    """
    base_url = f"{DOMAIN}/api/v1/accounts/{ACCOUNT_ID}/conversations"
    page = 1
    team_filter_supported = True
    while page <= max_pages:
        params = {
            "page": page,
            "per_page": per_page,
            "status": ",".join(statuses),
        }
        if team_id is not None and team_filter_supported:
            params["team_id"] = team_id

        payload = _get_json(base_url, params=params, timeout=TIMEOUT)
        if not payload:
            break

        convs = _extract_list_like(payload, prefer_keys=("data","payload","conversations","items","results"))
        if not convs:
            break

        for c in convs:
            if not isinstance(c, dict):
                continue
            if team_id is not None:
                c_team_id = c.get("team_id")
                if c_team_id is not None and str(c_team_id) != str(team_id):
                    continue
            yield c

        if len(convs) < per_page:
            break
        page += 1

def get_active_load_by_assignee(
    team_id: Union[str,int],
    *,
    statuses: Tuple[str,...] = ("open","pending")
) -> Dict[str, int]:
    """Retorna {assignee_id: quantidade_de_conversas_ativas} para o time."""
    counts: Dict[str, int] = {}
    for conv in _iter_conversations(team_id=team_id, statuses=statuses):
        aid = conv.get("assignee_id")
        if aid is None:
            continue
        key = str(aid)
        counts[key] = counts.get(key, 0) + 1
    return counts

def pick_least_loaded_assignee(
    team_id: Union[str,int],
    *,
    statuses: Tuple[str,...] = ("open","pending")
) -> Optional[str]:
    """
    Retorna o id (str) do agente menos carregado do time.
    Se não aparece no dict de carga, assume 0.
    Empate: menor id (determinístico).
    """
    members = get_team_members(team_id)
    if not members:
        return None

    loads = get_active_load_by_assignee(team_id, statuses=statuses)
    candidates: List[Tuple[int, str]] = []
    for m in members:
        aid = m["id"]
        load = loads.get(aid, 0)
        candidates.append((load, aid))

    if not candidates:
        return None

    candidates.sort(key=lambda t: (t[0], int(t[1]) if str(t[1]).isdigit() else str(t[1])))
    return candidates[0][1]

# ========================
# Simplificação de retorno
# ========================
def _pick_name(d: Dict[str, Any]) -> Optional[str]:
    if not isinstance(d, dict):
        return None
    return d.get("name") or d.get("available_name") or d.get("email")

def _simplify_conversation_payload(payload: Dict[str, Any], *, conversation_id_hint: Optional[Union[str,int]] = None) -> Dict[str, Any]:
    """
    Converte um payload de conversa (ou de POST /messages que retorna meta) em:
    {
      "conversation_id": int|str,
      "status": "open|pending|resolved|...",
      "priority": "low|medium|high|urgent|None",
      "assignee": {"id": int|str|None, "name": str|None},
      "team": {"id": int|str|None, "name": str|None}
    }
    """
    conv_id = payload.get("id") or conversation_id_hint
    status  = payload.get("status")
    priority = payload.get("priority")

    meta = payload.get("meta") if isinstance(payload, dict) else None
    assignee_meta = meta.get("assignee") if isinstance(meta, dict) else None
    team_meta = meta.get("team") if isinstance(meta, dict) else None

    assignee_top = payload.get("assignee")
    team_top = payload.get("team")

    assignee_id_solo = payload.get("assignee_id")
    team_id_solo = payload.get("team_id")

    # Resolve assignee
    assignee_id = (assignee_meta or {}).get("id") if isinstance(assignee_meta, dict) else None
    assignee_name = _pick_name(assignee_meta or {}) if assignee_meta else None
    if assignee_id is None and isinstance(assignee_top, dict):
        assignee_id = assignee_top.get("id")
        assignee_name = assignee_name or _pick_name(assignee_top)
    if assignee_id is None and assignee_id_solo is not None:
        assignee_id = assignee_id_solo
    if assignee_name is None and assignee_id is not None:
        assignee_name = str(assignee_id)

    # Resolve team
    team_id = (team_meta or {}).get("id") if isinstance(team_meta, dict) else None
    team_name = team_meta.get("name") if isinstance(team_meta, dict) else None
    if team_id is None and isinstance(team_top, dict):
        team_id = team_top.get("id")
        team_name = team_name or team_top.get("name")
    if team_id is None and team_id_solo is not None:
        team_id = team_id_solo
    if team_name is None and team_id is not None:
        team_name = str(team_id)

    # conv_id pode ser str não numérica — mantenha sem converter
    try:
        conv_id_out: Union[int, str] = int(conv_id) if str(conv_id).isdigit() else conv_id
    except Exception:
        conv_id_out = conv_id

    return {
        "conversation_id": conv_id_out,
        "status": status,
        "priority": priority,
        "assignee": {"id": assignee_id, "name": assignee_name},
        "team": {"id": team_id, "name": team_name},
    }

# ========================
# Mensagens / Fluxo de atribuição
# ========================
def _compose_private_message(*, motivo: str, prioridade: str, observacoes: Optional[str]) -> str:
    """
    Template:
      Motivo da transferência: {motivo objetivo}
      Prioridade: {urgent|high|medium|low}
      Observações: {detalhe útil}
    """
    obs_str = str(observacoes).strip() if observacoes is not None else "-"
    if not obs_str:
        obs_str = "-"
    return (
        f"Motivo da transferência: {motivo}\n"
        f"Prioridade: {prioridade}\n"
        f"Observações: {obs_str}"
    )

def get_conversation_simple(
    conversation_id: Union[str, int],
    *,
    timeout: int = TIMEOUT
) -> Dict[str, Any]:
    """
    Consulta o status simples da conversa por ID.
    """
    url = f"{DOMAIN}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}"
    payload = _get_json(url, timeout=timeout)
    if not payload:
        return {"error": "fetch_failed", "detail": "Não foi possível obter a conversa.", "conversation_id": conversation_id}
    return _simplify_conversation_payload(payload, conversation_id_hint=conversation_id)

def auto_assign_team_then_agent(
    conversation_id: Union[str, int],
    *,
    team_id: Union[str, int],
    motivo: str,
    prioridade: Union[str, int] = "low",
    observacoes: Optional[str] = None,
    statuses_for_load: Tuple[str, ...] = STATUSES_FOR_LOAD,
    timeout: int = TIMEOUT,
) -> Dict[str, Any]:
    """
    Passos:
      0) Se já tiver assignee -> retorna status (no-op).
      1) Atribui à equipe (sem assignee)
      2) Define prioridade (normalizada por PRIORITY_MAP)
      3) Se a prioridade estiver em CONFIG.auto_assign_by_priority -> escolhe agente menos carregado e atribui
      4) Envia mensagem privada
      5) Retorna o status simplificado
    """
    # 0) No-op se já está atribuído
    current = get_conversation_simple(conversation_id, timeout=timeout)
    if current.get("assignee", {}).get("id"):
        current.update({"step": "already_assigned"})
        return current

    base = f"{DOMAIN}/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}"

    # 1) Atribuir à EQUIPE (sem assignee)
    status_code, res = _post_json(
        f"{base}/assignments",
        {"team_id": team_id},
        timeout=timeout
    )
    if status_code == 0:
        res.update({"step": "assign_team"})
        return res
    if status_code >= 400:
        res.update({"step": "assign_team"})
        return res

    # 2) Definir prioridade (normaliza conforme PRIORITY_MAP)
    norm_priority = PRIORITY_MAP.get(prioridade, prioridade)
    norm_priority_str = str(norm_priority).lower()
    status_code, res = _post_json(
        f"{base}/toggle_priority",
        {"priority": norm_priority},
        timeout=timeout
    )
    if status_code == 0:
        res.update({"step": "priority"})
        return res
    if status_code >= 400:
        res.update({"step": "priority"})
        return res

    # 3) Atribuição automática condicionada à prioridade via CONFIG
    if norm_priority_str in AUTO_ASSIGN_PRIOS:
        chosen = pick_least_loaded_assignee(team_id, statuses=statuses_for_load)
        if not chosen:
            # Sem agente disponível: segue o fluxo (mantém só o time definido)
            pass
        else:
            status_code, res = _post_json(
                f"{base}/assignments",
                {"team_id": team_id, "assignee_id": chosen},
                timeout=timeout
            )
            if status_code == 0:
                res.update({"step": "assign_agent"})
                return res
            if status_code >= 400:
                res.update({"step": "assign_agent"})
                return res

    # 4) Mensagem privada no formato solicitado
    private_msg = _compose_private_message(
        motivo=motivo,
        prioridade=norm_priority_str,
        observacoes=observacoes,
    )
    status_code, res = _post_json(
        f"{base}/messages",
        {"private": True, "content": private_msg},
        timeout=timeout
    )
    if status_code == 0 or status_code >= 400:
        status_simple = get_conversation_simple(conversation_id, timeout=timeout)
        status_simple.update({"step": "message_error", "message_error": res})
        return status_simple

    # 5) Retorna o STATUS SIMPLIFICADO
    return get_conversation_simple(conversation_id, timeout=timeout)