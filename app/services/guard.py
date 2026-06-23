"""
Proteção contra prompt injection no Bob.

Estratégia em duas camadas:
  1. Guard de input — detecta padrões de injeção ANTES de enviar ao LLM.
     Mensagens claramente maliciosas são bloqueadas com HTTP 400.
     Mensagens suspeitas são registradas em log mas não bloqueadas.
  2. Hardening do system prompt — ver prompts.py (SYSTEM_BASE inclui
     instruções explícitas para o LLM ignorar meta-comandos do usuário).

Filosofia de false-positives:
  - Evitar bloquear perguntas legítimas de cultivo que acidentalmente
    contenham palavras como "ignore" ou "pretend" em contexto válido.
  - O bloqueio hard é reservado para padrões inequivocamente maliciosos.
  - Casos borderline são logados e passam, mas o system prompt endurecido
    faz com que o LLM os ignore naturalmente.
"""

import logging
import re

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

# ─── Padrões de injeção — hard block ─────────────────────────────────────────
#
# Expressões regulares que identificam tentativas clássicas de prompt injection.
# Qualquer match aqui resulta em HTTP 400 antes de tocar o LLM.

_HARD_BLOCK_PATTERNS: list[re.Pattern] = [
    # "Ignore previous/all/your instructions"
    re.compile(
        r"\bignore\b.{0,30}\b(previous|prior|above|all|your|system)\b.{0,30}\b(instructions?|prompts?|rules?|commands?|context)\b",
        re.IGNORECASE,
    ),
    # "Forget everything / forget your instructions"
    re.compile(
        r"\bforget\b.{0,30}\b(everything|all|your|what you|previous)\b",
        re.IGNORECASE,
    ),
    # "You are now [X] / Act as [X] / Pretend you are [X]"
    re.compile(
        r"\b(you are now|act as|pretend (to be|you are|you're)|roleplay as|simulate being)\b",
        re.IGNORECASE,
    ),
    # Jailbreak keywords conhecidos
    re.compile(
        r"\b(DAN|jailbreak|do anything now|developer mode|god mode|unrestricted mode|prison break)\b",
        re.IGNORECASE,
    ),
    # "Reveal / show / print your system prompt / instructions"
    re.compile(
        r"\b(reveal|show|print|display|output|repeat|write out)\b.{0,30}\b(system prompt|your instructions?|your rules?|your context|your programming)\b",
        re.IGNORECASE,
    ),
    # "Your new instructions are / New prompt:"
    re.compile(
        r"\b(your new (instructions?|prompt|rules?|context)|new (instructions?|prompt|system))\b.{0,10}:",
        re.IGNORECASE,
    ),
    # Tentativas de injeção via delimitadores de formato de LLM
    re.compile(
        r"(\[INST\]|<\|system\|>|<system>|###\s*system|<\|im_start\|>|\[\/INST\]|<\|endoftext\|>)",
        re.IGNORECASE,
    ),
    # "Override / bypass / circumvent the instructions"
    re.compile(
        r"\b(override|bypass|circumvent|disregard|dismiss)\b.{0,30}\b(instructions?|rules?|prompt|programming|restrictions?|guidelines?)\b",
        re.IGNORECASE,
    ),
    # Prompt injection via persona switching
    re.compile(
        r"\b(from now on|starting now|henceforth|for the rest of (this|the) (conversation|chat|session))\b.{0,60}\b(you (are|will|must|should)|respond|answer|act|behave)\b",
        re.IGNORECASE,
    ),
]

# ─── Padrões suspeitos — log only, não bloquear ───────────────────────────────
#
# Esses padrões são ambíguos — podem aparecer em contexto legítimo de cultivo.
# São apenas registrados em log para monitoramento futuro.

_SUSPICIOUS_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bsystem\s*prompt\b", re.IGNORECASE),
    re.compile(r"\byour (instructions?|programming|training)\b", re.IGNORECASE),
    re.compile(r"\bwhat (are|were) (your|the) (instructions?|rules?|guidelines?)\b", re.IGNORECASE),
    re.compile(r"\bconfidential\b.{0,40}\b(instructions?|prompt|rules?)\b", re.IGNORECASE),
    re.compile(r"\binitial prompt\b", re.IGNORECASE),
]

# ─── Limite de tamanho ────────────────────────────────────────────────────────

MAX_MESSAGE_CHARS = 2000  # limite razoável para um chat de consultoria


# ─── Função principal ─────────────────────────────────────────────────────────


_DEFAULT_INJECTION_MESSAGE = (
    "Mensagem nao permitida. O Bob e um consultor de cultivo e nao pode processar esse tipo de instrucao."
)


def guard_user_message(
    message: str,
    user_id: str = "",
    injection_message: str | None = None,
) -> str:
    """
    Valida e sanitiza a mensagem do usuário antes de enviar ao LLM.

    - Lança HTTPException 400 para mensagens com padrões de injeção confirmados.
    - Loga mensagens suspeitas mas as deixa passar (o system prompt hardened trata).
    - Trunca mensagens muito longas.
    - Retorna a mensagem sanitizada (stripped de espaços extras).

    Args:
        message: texto original do usuário
        user_id: para logging; não é obrigatório
        injection_message: mensagem personalizada para retornar quando bloqueado.
            Se None, usa _DEFAULT_INJECTION_MESSAGE.

    Returns:
        Mensagem validada e pronta para envio ao LLM.

    Raises:
        HTTPException 400 se a mensagem for claramente uma tentativa de injeção.
    """
    if not message or not message.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mensagem vazia.",
        )

    # Truncar mensagens excessivamente longas
    cleaned = message.strip()
    if len(cleaned) > MAX_MESSAGE_CHARS:
        logger.warning(
            "[guard] Mensagem truncada de %d para %d chars (user=%s)",
            len(cleaned), MAX_MESSAGE_CHARS, user_id,
        )
        cleaned = cleaned[:MAX_MESSAGE_CHARS]

    # Hard block — padrões inequivocamente maliciosos
    block_detail = injection_message or _DEFAULT_INJECTION_MESSAGE
    for pattern in _HARD_BLOCK_PATTERNS:
        if pattern.search(cleaned):
            logger.warning(
                "[guard] BLOCKED prompt injection attempt | pattern=%r | user=%s | msg_preview=%r",
                pattern.pattern[:60],
                user_id,
                cleaned[:120],
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=block_detail,
            )

    # Suspicious — log only
    for pattern in _SUSPICIOUS_PATTERNS:
        if pattern.search(cleaned):
            logger.info(
                "[guard] suspicious message (not blocked) | user=%s | msg_preview=%r",
                user_id,
                cleaned[:120],
            )
            break  # um log por mensagem é suficiente

    return cleaned
