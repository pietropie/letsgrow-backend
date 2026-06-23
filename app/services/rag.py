import logging
import time
from datetime import date, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.ai_config import AIConfig
from app.models.grow import Grow
from app.models.knowledge import AIConversation, KnowledgeChunk
from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorReading
from app.rag.prompts import build_customer_context, build_grow_context, build_plant_context, build_system_prompt
from app.rag.retriever import retrieve_chunks
from app.services import ai_provider

logger = logging.getLogger(__name__)

# ─── Cache de configuração + clients de IA ───────────────────────────────────
#
# A config (provider/modelo/dimensões) agora vive na tabela ai_config e é
# editável em runtime pelo painel admin (/admin/ai-panel) — sem redeploy.
#
# Para não bater no banco a cada mensagem de chat / chunk indexado, mantemos
# um cache em memória do processo com TTL curto. Quando o TTL expira, relemos
# a config; se ela mudou desde a última leitura, reconstruímos os clients
# (LangChain) — caso contrário reaproveitamos os já instanciados.
_CACHE_TTL_SECONDS = 60

_cache: dict = {
    "config": None,
    "llm": None,
    "embeddings": None,
    "fetched_at": 0.0,
}


def _fallback_config() -> AIConfig:
    """Usado apenas se a tabela ai_config estiver vazia (não deveria acontecer
    após a migration de seed — é uma rede de segurança)."""
    return AIConfig(
        provider="gemini",
        chat_model=settings.GEMINI_MODEL,
        temperature=0.3,
        embedding_provider="gemini",
        embedding_model=settings.EMBEDDING_MODEL,
        embedding_dimensions=settings.EMBEDDING_DIMENSIONS,
    )


def _signature(config: AIConfig) -> tuple:
    return (
        config.provider,
        config.chat_model,
        config.temperature,
        config.embedding_provider,
        config.embedding_model,
        config.embedding_dimensions,
    )


async def _load_config(db: AsyncSession) -> AIConfig:
    result = await db.execute(select(AIConfig).order_by(AIConfig.created_at.asc()).limit(1))
    config = result.scalar_one_or_none()
    return config if config is not None else _fallback_config()


async def _refresh_cache(db: AsyncSession) -> AIConfig:
    config = await _load_config(db)
    rebuild = _cache["config"] is None or _signature(_cache["config"]) != _signature(config)

    if rebuild:
        logger.info(
            "Config de IA (re)carregada — chat: %s/%s | embeddings: %s/%s (%dd)",
            config.provider, config.chat_model,
            config.embedding_provider, config.embedding_model, config.embedding_dimensions,
        )
        _cache["llm"] = ai_provider.build_llm(config.provider, config.chat_model, config.temperature)
        _cache["embeddings"] = ai_provider.build_embeddings(config.embedding_provider, config.embedding_model)

    # Armazena uma cópia transient (desvinculada de qualquer Session) para
    # evitar o erro "Instance is not bound to a Session" quando o cache é
    # acessado em requisições futuras (onde a sessão original já fechou).
    _cache["config"] = AIConfig(
        provider=config.provider,
        chat_model=config.chat_model,
        temperature=config.temperature,
        embedding_provider=config.embedding_provider,
        embedding_model=config.embedding_model,
        embedding_dimensions=config.embedding_dimensions,
    )
    _cache["fetched_at"] = time.monotonic()
    return _cache["config"]


async def get_ai_context(db: AsyncSession) -> tuple[AIConfig, object, object]:
    """
    Retorna (config, llm, embeddings) já prontos para uso, lendo da ai_config
    no banco e respeitando o cache de ~60s. É o ponto único de entrada usado
    por chat() (abaixo), retriever.py e scripts/index_brain.py — assim, trocar
    o provedor/modelo no painel admin reflete em todo o app sem redeploy.
    """
    now = time.monotonic()
    if _cache["config"] is None or (now - _cache["fetched_at"]) > _CACHE_TTL_SECONDS:
        await _refresh_cache(db)
    return _cache["config"], _cache["llm"], _cache["embeddings"]


def invalidate_cache() -> None:
    """Chamado pelo endpoint PUT /admin/ai-config logo após salvar uma nova
    config, para que a mudança valha já na próxima requisição (em vez de
    esperar até 60s pelo TTL natural)."""
    _cache["config"] = None
    _cache["fetched_at"] = 0.0


async def chat(
    db: AsyncSession,
    conversation: AIConversation,
    user_message: str,
    grow: Grow | None = None,
    plant: Plant | None = None,
    images: list[str] | None = None,
) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

    # Contexto completo do cliente (todos grows + plantas + eventos recentes)
    # injetado em TODA conversa, independentemente de grow/planta selecionados.
    customer_ctx = await build_customer_context(db, conversation.user_id)

    # Contexto focado: planta específica (com grow vinculado) ou grow selecionado
    plant_ctx = ""
    grow_ctx = ""
    if plant:
        plant_grow: Grow | None = None
        if plant.grow_id:
            plant_grow = await db.get(Grow, plant.grow_id)
        plant_ctx = await build_plant_context(db, plant, plant_grow)
    elif grow:
        grow_ctx = await build_grow_context(db, grow)

    # Retrieve relevant knowledge chunks
    chunks = await retrieve_chunks(db, user_message, grow, top_k=4)
    rag_context = "\n\n---\n\n".join(c.content for c in chunks)

    system_prompt = build_system_prompt(grow_ctx, rag_context, plant_ctx, customer_ctx)

    # Build message history (last 6 messages to save tokens)
    history = conversation.messages[-6:] if conversation.messages else []
    lc_messages: list = [SystemMessage(content=system_prompt)]
    for msg in history:
        if msg["role"] == "assistant":
            lc_messages.append(AIMessage(content=msg["content"]))
        else:
            lc_messages.append(HumanMessage(content=msg["content"]))

    # Last human message — multimodal if images were sent
    images = [img for img in (images or []) if img.startswith("data:image/")][:4]
    _, llm, _ = await get_ai_context(db)

    if images:
        content_blocks: list[dict] = [{"type": "text", "text": user_message}]
        for data_uri in images:
            content_blocks.append({"type": "image_url", "image_url": {"url": data_uri}})
        lc_messages.append(HumanMessage(content=content_blocks))
        try:
            response = await llm.ainvoke(lc_messages)
            return response.content
        except Exception as vision_err:
            logger.warning(
                "Falha ao processar imagens com o LLM configurado (%s). "
                "Retentando sem imagens.", vision_err
            )
            # Fallback: remove o bloco multimodal e tenta só com texto
            lc_messages[-1] = HumanMessage(content=user_message)
            try:
                response = await llm.ainvoke(lc_messages)
                return (
                    response.content
                    + "\n\n_(Obs.: não consegui analisar as imagens — o modelo de IA configurado "
                    "pode não ter suporte a visão. Peça ao administrador para configurar "
                    "Gemini 1.5 Flash ou Claude no painel de IA.)_"
                )
            except Exception as text_err:
                logger.error("Falha total no chat, inclusive sem imagens: %s", text_err)
                raise
    else:
        lc_messages.append(HumanMessage(content=user_message))
        response = await llm.ainvoke(lc_messages)
        return response.content


# ─── Dicas contextuais do Bob ────────────────────────────────────────────────
#
# Motor híbrido: regras determinísticas detectam o cenário relevante para a
# planta (baseadas em datas, histórico de eventos e fases), e o LLM formula
# a dica em linguagem natural no estilo Bob. O resultado fica em cache por
# TIP_CACHE_TTL segundos para não chamar o LLM a cada reload de tela.

_TIP_CACHE_TTL = 4 * 3600  # 4 horas
_tip_cache: dict = {}   # plant_id_str -> {"scenario": str, "response": dict, "at": float}


def _detect_tip_scenario(plant: Plant, events: list) -> dict | None:
    """Regras determinísticas que detectam o cenário mais relevante da planta."""
    from datetime import date as _date

    today = _date.today()

    germ_days: int | None = None
    if plant.germination_date:
        germ_days = (today - plant.germination_date).days

    flip_days: int | None = None
    if plant.flip_date:
        flip_days = (today - plant.flip_date).days

    expected_harvest = plant.expected_harvest_days or 63  # padrão conservador para foto

    # Ordena eventos do mais recente para o mais antigo
    sorted_evts = sorted(events, key=lambda e: e.event_date, reverse=True)
    waterings = [e for e in sorted_evts if e.event_type in ("rega", "watering")]

    # ── 1. Pré-colheita (urgente) ──
    if plant.current_phase == "flower" and flip_days is not None:
        days_left = expected_harvest - flip_days
        if 0 <= days_left <= 10:
            return {
                "scenario": "pre_harvest",
                "priority": "urgent",
                "icon": "🌾",
                "flip_days": flip_days,
                "days_left": days_left,
                "expected_harvest": expected_harvest,
            }

    # ── 2. Flip recomendado (vegetativo >= 45 dias) ──
    if plant.current_phase == "veg" and germ_days is not None and germ_days >= 45:
        return {
            "scenario": "flip_soon",
            "priority": "warning",
            "icon": "⚡",
            "germ_days": germ_days,
        }

    # ── 3. Rega atrasada ou fertilização devida ──
    if waterings:
        last_w = waterings[0]
        # event_date pode ser datetime ou date
        last_w_date = last_w.event_date.date() if hasattr(last_w.event_date, "date") else last_w.event_date
        days_since = (today - last_w_date).days
        if days_since >= 3:
            # Conta regas consecutivas sem fertilizante (flush ou sem PPM)
            consecutive_flush = 0
            for w in waterings[:5]:
                if getattr(w, "is_flush", False) or not getattr(w, "ppm", None):
                    consecutive_flush += 1
                else:
                    break
            if consecutive_flush >= 2:
                return {
                    "scenario": "fert_due",
                    "priority": "info",
                    "icon": "🌿",
                    "days_since_water": days_since,
                    "consecutive_flush": consecutive_flush,
                }
            return {
                "scenario": "water_due",
                "priority": "warning",
                "icon": "💧",
                "days_since_water": days_since,
            }

    # ── 4. Seedling (< 14 dias) ──
    if germ_days is not None and germ_days < 14:
        return {
            "scenario": "seedling",
            "priority": "info",
            "icon": "🌱",
            "germ_days": germ_days,
        }

    return None


def _build_tip_prompt(plant: Plant, scenario: dict) -> str:
    base = (
        f"Você é Bob, consultor especialista em cannabis. "
        f"Em 2 a 3 frases curtas, diretas e amigáveis, dê uma dica ao cultivador "
        f"sobre a planta {plant.strain_name} (fase: {plant.current_phase}). "
        f"Sem emojis, sem saudação, sem 'Bob aqui'. Responda SOMENTE o texto da dica.\n\n"
        f"Contexto: "
    )
    s = scenario["scenario"]
    if s == "pre_harvest":
        return base + (
            f"A planta está em floração há {scenario['flip_days']} dias com previsão de "
            f"{scenario['expected_harvest']} dias. Faltam ~{scenario['days_left']} dias para a colheita. "
            f"Dê uma dica de preparação (tricomas, lavagem, etc.)."
        )
    if s == "flip_soon":
        return base + (
            f"A planta está com {scenario['germ_days']} dias no vegetativo. "
            f"Recomende ao cultivador considerar o flip 12/12 em breve."
        )
    if s == "water_due":
        return base + (
            f"A última rega foi há {scenario['days_since_water']} dias. "
            f"Alerte o cultivador para verificar se a planta precisa de água."
        )
    if s == "fert_due":
        return base + (
            f"As últimas {scenario['consecutive_flush']} regas foram só com água (sem nutrientes). "
            f"Recomende uma rega com fertilizante na próxima vez."
        )
    if s == "seedling":
        return base + (
            f"A planta está com {scenario['germ_days']} dias desde a germinação (fase seedling). "
            f"Dê uma dica rápida de cuidados nesta fase."
        )
    return base + "Dê uma dica geral de cultivo para esta planta."


def _template_tip(scenario: dict) -> str:
    """Fallback sem LLM — usado quando o modelo falha ou timeout."""
    s = scenario["scenario"]
    if s == "pre_harvest":
        dl = scenario.get("days_left", "?")
        return (
            f"Faltam aproximadamente {dl} dias para a colheita. "
            f"Comece a observar os tricomas e prepare a lavagem do substrato se necessário."
        )
    if s == "flip_soon":
        gd = scenario.get("germ_days", "?")
        return (
            f"Sua planta está com {gd} dias de vegetativo — "
            f"considere fazer o flip para 12/12 em breve para iniciar a floração."
        )
    if s == "water_due":
        ds = scenario.get("days_since_water", "?")
        return (
            f"Faz {ds} dias desde a última rega. "
            f"Verifique se o substrato está seco e se a planta precisa de água."
        )
    if s == "fert_due":
        cf = scenario.get("consecutive_flush", "?")
        return (
            f"As últimas {cf} regas foram só com água. "
            f"Considere uma rega com fertilizante para repor os nutrientes."
        )
    if s == "seedling":
        gd = scenario.get("germ_days", "?")
        return (
            f"Sua planta está com {gd} dias na fase seedling. "
            f"Mantenha umidade entre 60-70% e evite nutrientes fortes por enquanto."
        )
    return "Verifique sua planta hoje e registre um evento no diário."


async def generate_plant_tip(db, *, plant: Plant, events: list) -> dict | None:
    """Detecta cenário + gera dica via LLM com cache de 4h. Retorna dict ou None."""
    import time as _time
    from langchain_core.messages import HumanMessage

    scenario = _detect_tip_scenario(plant, events)
    if scenario is None:
        return None

    plant_key = str(plant.id)
    cached = _tip_cache.get(plant_key)
    if (
        cached
        and (_time.monotonic() - cached["at"]) < _TIP_CACHE_TTL
        and cached["scenario"] == scenario["scenario"]
    ):
        return cached["response"]

    # Gera via LLM; fallback para template se falhar
    prompt = _build_tip_prompt(plant, scenario)
    try:
        _, llm, _ = await get_ai_context(db)
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        tip_text = resp.content.strip()
    except Exception as exc:
        logger.warning("Bob tip LLM falhou (%s) — usando template", exc)
        tip_text = _template_tip(scenario)

    result = {
        "scenario": scenario["scenario"],
        "tip": tip_text,
        "priority": scenario["priority"],
        "icon": scenario["icon"],
    }
    _tip_cache[plant_key] = {"scenario": scenario["scenario"], "response": result, "at": _time.monotonic()}
    return result


def invalidate_plant_tip_cache(plant_id) -> None:
    """Chame após criar evento para forçar regerar a dica na próxima requisição."""
    _tip_cache.pop(str(plant_id), None)


# ─── Análise de fotos de eventos do diário (multimodal) ──────────────────────
#
# Usa o mesmo `llm` resolvido via get_ai_context (cache de ~60s, configurável
# pelo painel admin) — mas aqui montamos uma mensagem multimodal (texto + N
# imagens em base64) seguindo o formato padrão de "content blocks" do
# LangChain (`image_url` com data URI), suportado nativamente por
# ChatGoogleGenerativeAI/ChatOpenAI/ChatAnthropic. Provedores sem suporte a
# visão (DeepSeek/Z.ai) vão simplesmente falhar na chamada — é o admin quem
# escolhe o provider/modelo em /admin/ai-panel, então recomendamos Gemini.

def _build_photo_analysis_prompt(plant: Plant, event, grow: Grow | None = None) -> str:
    """Constrói o prompt de análise multimodal com contexto completo da planta e grow."""
    from datetime import date as _date

    today = _date.today()

    # ── Contexto da planta ───────────────────────────────────────────────────
    genetics_str = f" ({plant.genetics})" if plant.genetics else ""
    ctx_lines = [f"Planta: {plant.strain_name}{genetics_str} | Tipo: {plant.strain_type}"]

    phase_detail = plant.current_phase
    if plant.flip_date and plant.current_phase == "flower":
        flip_days = (today - plant.flip_date).days
        phase_detail = f"floração — {flip_days} dias de flora"
        if plant.expected_harvest_days:
            remaining = plant.expected_harvest_days - flip_days
            if remaining > 0:
                phase_detail += f" (previsão: ~{remaining} dias restantes)"
    elif plant.germination_date:
        germ_days = (today - plant.germination_date).days
        phase_detail = f"{plant.current_phase} — dia {germ_days}"

    ctx_lines.append(f"Fase: {phase_detail}")
    ctx_lines.append(f"Evento registrado: {event.event_type}")

    if plant.substrate:
        ctx_lines.append(f"Substrato: {plant.substrate}")
    if plant.pot_volume_liters:
        ctx_lines.append(f"Volume do vaso: {plant.pot_volume_liters}L")

    # ── Dados do evento analisado ────────────────────────────────────────────
    event_data = []
    if event.ppm:
        event_data.append(f"PPM: {event.ppm}")
    if event.ph_in:
        event_data.append(f"pH entrada: {event.ph_in}")
    if event.ph_out:
        event_data.append(f"pH saída: {event.ph_out}")
    if event.temperature_c:
        event_data.append(f"Temperatura: {event.temperature_c}°C")
    if event.humidity_rh:
        event_data.append(f"Umidade: {event.humidity_rh}%")
    if event_data:
        ctx_lines.append(f"Medições deste evento: {' | '.join(event_data)}")
    if event.notes:
        ctx_lines.append(f"Observações do cultivador: {event.notes}")

    # ── Ambiente do grow ─────────────────────────────────────────────────────
    if grow:
        env_parts = []
        if grow.lighting_watts:
            light_str = f"{grow.lighting_watts}W"
            if grow.light_type:
                light_str += f" {grow.light_type}"
            env_parts.append(light_str)
        if grow.photoperiod_hours:
            env_parts.append(f"fotoperíodo {grow.photoperiod_hours}")
        if grow.tent_width_cm:
            env_parts.append(f"tent {grow.tent_width_cm}×{grow.tent_depth_cm} cm")
        if grow.substrate_type:
            env_parts.append(f"substrato {grow.substrate_type}")
        if env_parts:
            ctx_lines.append(f"Ambiente: {' | '.join(env_parts)}")

    plant_context = "\n".join(ctx_lines)

    return (
        "Você é Bob, consultor especialista em cultivo de cannabis com linguagem acessível e direta.\n\n"
        f"## Contexto do Cultivo\n{plant_context}\n\n"
        "## Tarefa\n"
        "Analise as fotos enviadas acima e responda SOMENTE com um objeto JSON válido — sem texto antes ou depois do JSON:\n"
        '{\n'
        '  "status": "saudavel" ou "atencao" ou "critico",\n'
        '  "resumo": "1 a 2 frases descrevendo o estado geral da planta",\n'
        '  "problemas": ["problema visível 1", "problema visível 2"],\n'
        '  "recomendacoes": ["recomendacao pratica 1", "recomendacao pratica 2"],\n'
        '  "observacao_foto": null ou "nota caso a qualidade/angulo das fotos limite a analise"\n'
        '}\n\n'
        "Regras:\n"
        "- Liste SOMENTE o que e claramente visivel nas imagens (sem especulacao).\n"
        "- Use o contexto do cultivo acima para dar recomendacoes mais precisas.\n"
        "- Se a planta parecer saudavel, 'problemas' deve ser lista vazia [].\n"
        "- Use linguagem simples que um cultivador iniciante entenda.\n"
        "- Se a qualidade das fotos nao permitir analise confiavel, indique em 'observacao_foto'."
    )


def _read_photo_object(object_key: str) -> tuple[bytes, str]:
    """Le os bytes de uma foto direto do MinIO (client interno - roda em thread,
    pois o SDK do MinIO e sincrono)."""
    from app.services.storage import BUCKET_EVENTS, get_minio_client

    response = get_minio_client().get_object(BUCKET_EVENTS, object_key)
    try:
        data = response.read()
        content_type = response.headers.get("Content-Type") or "image/jpeg"
        return data, content_type
    finally:
        response.close()
        response.release_conn()


async def analyze_event_photos(db: AsyncSession, *, plant: Plant, event) -> dict:
    """Envia as fotos de um evento do diario para o LLM multimodal configurado
    e retorna dict estruturado com status, resumo, problemas e recomendacoes."""
    import asyncio
    import base64

    from langchain_core.messages import HumanMessage

    # Carrega o grow vinculado a planta para enriquecer o prompt
    grow: Grow | None = None
    if plant.grow_id:
        grow = await db.get(Grow, plant.grow_id)

    content_blocks: list[dict] = [
        {"type": "text", "text": _build_photo_analysis_prompt(plant, event, grow)}
    ]

    for object_key in (event.photo_keys or [])[:6]:
        try:
            data, content_type = await asyncio.to_thread(_read_photo_object, object_key)
        except Exception as exc:
            logger.warning("Falha ao baixar foto %s para analise de IA: %s", object_key, exc)
            continue
        b64 = base64.b64encode(data).decode("ascii")
        content_blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:{content_type};base64,{b64}"},
        })

    if len(content_blocks) == 1:
        raise RuntimeError("Nao foi possivel carregar nenhuma das fotos deste evento para analise.")

    _, llm, _ = await get_ai_context(db)
    response = await llm.ainvoke([HumanMessage(content=content_blocks)])
    text = response.content

    # Tenta extrair JSON da resposta (o LLM as vezes envolve em ```json ... ```)
    import json as _json
    try:
        start = text.find('{')
        end = text.rfind('}') + 1
        if start == -1 or end == 0:
            raise ValueError("Nenhum objeto JSON encontrado na resposta do LLM")
        data = _json.loads(text[start:end])
        # Garante que os campos obrigatorios existem com tipos corretos
        return {
            "status": str(data.get("status", "atencao")),
            "resumo": str(data.get("resumo", text[:200])),
            "problemas": [str(p) for p in data.get("problemas", [])],
            "recomendacoes": [str(r) for r in data.get("recomendacoes", [])],
            "observacao_foto": data.get("observacao_foto") or None,
        }
    except Exception as parse_err:
        logger.warning("Falha ao parsear JSON do Bob (%s) -- usando resposta raw como resumo", parse_err)
        return {
            "status": "atencao",
            "resumo": text[:300],
            "problemas": [],
            "recomendacoes": [],
            "observacao_foto": "Resposta nao estruturada -- exibindo texto original.",
        }
