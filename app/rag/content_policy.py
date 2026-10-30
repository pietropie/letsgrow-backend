"""
Limites de conteúdo da base de conhecimento do Bobby (política da Google Play).

A Play proíbe app que facilite a venda de cannabis (lojas, preços, entrega), e o
relatório de política (analise-2026-10-16/PLAYSTORE.md §2) também tira do Bobby
extração, formas e efeitos de consumo e promessas terapêuticas. A wiki (Brain/)
continua como está; o filtro vale em três pontos:

- indexação (`should_index` e `sanitize_markdown` em app/rag/indexer.py): arquivos
  inteiros que não entram e seções/frases cortadas antes de gerar os chunks;
- busca (app/services/rag.py): o mesmo corte no texto recuperado, para chunks
  antigos ainda não reindexados e para os aprovados de conversas;
- catálogo de strains (`sanitize_text` no resumo, app/services/strain_catalog.py).
"""
import re

# Arquivos inteiros fora do RAG: lojas de sementes (URL, preço, frete), extração,
# documentos de produto/negócio (catálogo de lojas, personas de consumo) e o texto
# sobre habeas corpus, que defende desobediência civil.
BLOCKED_PATHS = (
    "markets/",
    "extractions/",
    "sources/netseeds-",
    "sources/flora-urbana-",
    "sources/seeds-genetics-brazil-",
    "concepts/mapa-dores-grower.md",
    "concepts/personas-usuario.md",
    "concepts/seguranca-juridica-hc.md",
)

# Seções cortadas pelo título (com as subseções): disponibilidade/preço nas lojas,
# efeitos e usos de consumo, extração, uso medicinal e farmacologia do consumo.
_BLOCKED_HEADING = re.compile(
    r"dispon[ií]vel no brasil|^disponibilidade$|onde comprar sementes|efeitos? e usos|extra[çc](ão|ões|ao|oes)"
    r"|uso medicinal|usos medicinais|valor terap|medicamentos aprovados|farmacocin|vias de administra"
    r"|sistema endocanabinoide|perfil de seguran[çc]a|desobedi[êe]ncia civil|inqu[ée]ritos cient"
    r"|paciente medicinal",
    re.IGNORECASE,
)

# Frases (ou linhas de tabela) cortadas onde quer que apareçam.
_BLOCKED_TEXT = re.compile(
    "|".join([
        # venda: lojas, preço, frete, disponibilidade e links para as páginas bloqueadas
        r"net\s?seeds", r"flora urbana", r"seeds genetics", r"seis p[ée]s", r"pur[êe] instinto",
        r"\bdispon[ií]ve(l|is) (na|nas|no|nos|em) lojas?\b", r"\bfrete\b", r"\bcupom\b",
        r"cat[áa]logo de lojas", r"comparativo de pre[çc]os", r"lojas? (de sementes|que comercializam)",
        r"\[\[(markets|extractions)/", r"\[\[sources/(netseeds|flora-urbana|seeds-genetics-brazil)",
        r"\[\[concepts/(mapa-dores-grower|personas-usuario|seguranca-juridica-hc)",
        # extração e concentrados
        r"\brosin\b", r"bubble[\s-]?hash", r"dry[\s-]?sift", r"\bbho\b", r"\bkief\b", r"\bhaxixe\b",
        r"\bhash\b(?!\s+plant)", r"fresh frozen", r"\bpara extra[çc][ãa]o\b", r"extra[çc][ãa]o por compress",
        r"\bextratos?\b(?!\s+de\s+(algas?|levedura|húmus|humus))", r"competi[çc][õo]es de concentrado",
        # consumo e efeitos
        r"\bfum(ar|o|ado|ada|a|antes?)\b", r"vaporiz", r"\bedibles?\b", r"\bbuzz\b",
        r"\bbrisa\b", r"\bchapad", r"psicoativ", r"potencializa (o )?thc",
        r"efeitos? (reportados?|sedativ|relaxant|eufóric|euforic|corporal|cerebral|mais corporal|mais cerebral|"
        r"equilibrado|calmante|energ)", r"uso (diurno|noturno|recreativo|adulto|vespertino)",
        r"vias? de (consumo|administra)",
        # promessas terapêuticas; condições em lista ("dor, ansiedade, insônia"), porque
        # "ansiedade" sozinha também é a do cultivador ("não colher cedo por ansiedade")
        r"usos? medicina", r"\b(dor|ansiedade|ins[ôo]nia|depress[ãa]o)\s*,\s*(dor|ansiedade|ins[ôo]nia|depress[ãa]o|estresse)\b",
        r"ansiol[íi]tic", r"\bins[ôo]nia\b",
        r"\bdor (cr[ôo]nica|leve|moderada|intensa)", r"\bepilepsia\b", r"\bn[áa]usea", r"terap[êe]utic",
        r"\bsedativ", r"\brelaxante", r"anti-?inflamat", r"broncodilat", r"supressor de apetite",
        r"eleva (o )?humor", r"clareza mental",
    ]),
    re.IGNORECASE,
)

# Qualificadores de efeito no meio de uma informação de cultivo ("Colher (efeito mais
# cerebral)", "THC → CBN (canabinoide sedativo)"): saem só eles e a linha fica.
_SOFT_REDACT = re.compile(
    r"\s*\(?\b(com |para )?(leve |um )?efeitos? (mais |muito |bem )?(corporal|cerebral|sedativo|equilibrado|"
    r"energ\w*|pesado|corpo)(/\w+)?( demais)?\b\)?|\s*\(canabinoide sedativo\)|,\s*sedativo\b",
    re.IGNORECASE,
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ\[\*\"“(])")
_PREFIX = re.compile(r"^[\s>*\-+\d.]*(\*\*[^*]+\*\*\s*:?\s*)?")
_LABEL_ONLY = re.compile(r"^[\s>*\-+\d.]*(\*\*[^*]+\*\*\s*:?)?\s*:?\s*$")


def is_blocked_path(rel_path: str) -> bool:
    return any(blocked in rel_path for blocked in BLOCKED_PATHS)


def is_blocked_text(text: str) -> bool:
    return bool(_BLOCKED_TEXT.search(text))


def is_blocked_tag(tag: str) -> bool:
    return is_blocked_text(tag) or bool(re.search(r"concentrad|extra[çc]|medicinal|loja", tag, re.IGNORECASE))


def _clean_line(line: str) -> str | None:
    """A linha sem as frases bloqueadas; None se não sobrar nada útil."""
    if _SOFT_REDACT.search(line):
        line = re.sub(r"\(\s*[,;]?\s*\)|,\s*(?=\))", "", _SOFT_REDACT.sub("", line))
    if not is_blocked_text(line):
        return line
    if line.lstrip().startswith("|"):
        return None  # linha de tabela: sai inteira
    parts = _SENTENCE_SPLIT.split(line)
    kept = [s for s in parts if not is_blocked_text(s)]
    if not kept:
        return None
    cleaned = " ".join(kept)
    if kept[0] is not parts[0]:
        # A primeira frase saiu: mantém o marcador de lista e o rótulo em negrito dela
        cleaned = _PREFIX.match(parts[0]).group(0) + cleaned
    return None if _LABEL_ONLY.match(cleaned) else cleaned


def _drop_empty_tables(lines: list[str]) -> list[str]:
    out: list[str] = []
    block: list[str] = []
    for line in lines + [""]:
        if line.lstrip().startswith("|"):
            block.append(line)
            continue
        if block:
            # Só cabeçalho e separador: a tabela ficou sem linhas de dados
            if len(block) > 2:
                out.extend(block)
            block = []
        out.append(line)
    return out[:-1]


def sanitize_markdown(text: str) -> str:
    """Tira de um markdown as seções e frases fora da política. Idempotente."""
    out: list[str] = []
    skip_level: int | None = None
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if skip_level is not None and level > skip_level:
                continue
            skip_level = None
            if _BLOCKED_HEADING.search(heading.group(2)) or is_blocked_text(heading.group(2)):
                skip_level = level
                continue
            out.append(line)
            continue
        if skip_level is not None:
            continue
        cleaned = _clean_line(line)
        if cleaned is not None:
            out.append(cleaned)
    result = "\n".join(_drop_empty_tables(out))
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def sanitize_text(text: str | None) -> str | None:
    """Versão para um parágrafo solto (resumo da strain): só corta frases."""
    if text is None:
        return None
    kept = [s for s in _SENTENCE_SPLIT.split(text) if not is_blocked_text(s)]
    return " ".join(kept).strip() or None
