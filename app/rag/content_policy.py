"""
Limites de conteúdo da base de conhecimento do Bobby (política da Google Play).

A Play proíbe app que facilite a venda de cannabis (lojas, preços, entrega), e o
relatório de política (analise-2026-10-16/PLAYSTORE.md §2) também tira do Bobby
extração, formas de consumo, dose e promessas terapêuticas. Desde a opção A
(13/11/2026, §2.2) o perfil de efeito relatado, o aroma e o sabor passam; gíria,
ranking de potência, ocasião de uso e condição de saúde continuam cortados.
A wiki (Brain/) continua como está; o filtro vale em três pontos:

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
    # Livro de farmacologia: efeitos no corpo, vias de consumo, dependência e uso médico
    "sources/iversen-science-of-marijuana-",
)

# Seções cortadas pelo título (com as subseções): disponibilidade/preço nas lojas,
# extração, uso medicinal e farmacologia do consumo. "Efeitos e Usos" fica (opção A,
# 13/11/2026): dentro dela, a linha "Usos medicinais" sai pelas frases bloqueadas.
_BLOCKED_HEADING = re.compile(
    r"dispon[ií]vel no brasil|^disponibilidade$|onde comprar sementes|extra[çc](ão|ões|ao|oes)"
    r"|uso medicinal|usos medicinais|valor terap|medicamentos aprovados|farmacocin|vias de administra"
    r"|sistema endocanabinoide|perfil de seguran[çc]a|desobedi[êe]ncia civil|inqu[ée]ritos cient"
    r"|paciente medicinal|riscos reais|toxicidade|compara[çc][ãa]o com [áa]lcool|receptores canabinoides"
    r"|endocanabinoides|mecanismo de a[çc][ãa]o",
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
        r"\[\[concepts/(mapa-dores-grower|personas-usuario|seguranca-juridica-hc)", r"\[\[sources/iversen-",
        # extração e concentrados
        r"\brosin\b", r"bubble[\s-]?hash", r"dry[\s-]?sift", r"\bbho\b", r"\bkief\b", r"\bhaxixe\b",
        r"\bhash\b(?!\s+plant)", r"fresh frozen", r"\bpara extra[çc][ãa]o\b", r"extra[çc][ãa]o por compress",
        r"\bextratos?\b(?!\s+de\s+(algas?|levedura|húmus|humus))", r"competi[çc][õo]es de concentrado",
        # consumo: forma, ocasião de uso, gíria e potência. O perfil de efeito relatado
        # (relaxante, energizante, sedativo, foco), o aroma e o sabor ficam (PLAYSTORE.md §2.2).
        r"\bfum(ar|o|ado|ada|a|antes?)\b", r"vaporiz", r"\bedibles?\b", r"\bbuzz\b",
        r"\bbrisa\b", r"\bchapad", r"\bbate forte\b", r"\bhigh (sativa|indica|cerebral|corporal|potente|forte|intenso)", r"\bcouch-?lock",
        r"psicoativ", r"potencializa (o )?thc", r"\b(a|o|as|os) mais (fortes?|potentes?)\b", r"\bque mais derruba",
        r"uso (diurno|noturno|recreativo|adulto|vespertino|social|matinal)", r"para (festa|baladas?|dormir|relaxar depois)",
        r"wake and bake", r"atividades? diurnas?", r"para o dia todo", r"\bavassalador",
        r"(uso|usu[áa]rios?) por pessoas experientes",
        # dose no texto de efeito ("em doses maiores"); "doses altas" de fertilizante fica
        r"\b(em|nas|a) doses\b", r"\bdoses? (equivalentes|maiores|menores|normais)\b",
        r"vias? de (consumo|administra)",
        # farmacologia e riscos do uso (efeito no corpo, dependência, saúde mental)
        r"endocanabin", r"\bCB-?[12]\b", r"agonista", r"anandamida", r"11-hidroxi", r"\bdl50\b", r"carcinog",
        r"overdose (fatal|de thc|de cannabis)", r"psicose", r"psic[óo]tic", r"esquizofren", r"abstin[êe]ncia",
        # "dependência" do uso, não a do cultivo ("dependência do fotoperíodo", "de insumos")
        r"depend[êe]ncia (de|à|a|ao|em) (cannabis|maconha|thc|subst[âa]ncias?)", r"desenvolve\w* depend[êe]ncia",
        r"(causa|gera|risco de|crit[ée]rio de) depend[êe]ncia", r"meia-vida do thc",
        r"teste de urina", r"\bvia oral\b", r"\binala(do|da|ção|cao|r)\b", r"\d+(-\d+)? baseados",
        r"usu[áa]rios (de cannabis|pesados|di[áa]rios|cumulativos)", r"mem[óo]ria de curto prazo",
        r"(intensidade|dura[çc][ãa]o) do efeito", r"cannabis crua",
        # promessas terapêuticas; condições em lista ("dor, ansiedade, insônia"), porque
        # "ansiedade" sozinha também é a do cultivador ("não colher cedo por ansiedade")
        r"usos? medicina", r"\b(dor|ansiedade|ins[ôo]nia|depress[ãa]o)\s*,\s*(dor|ansiedade|ins[ôo]nia|depress[ãa]o|estresse)\b",
        r"ansiol[íi]tic", r"\bins[ôo]nia\b",
        r"\bdor (cr[ôo]nica|leve|moderada|intensa)", r"\bepilepsia\b", r"\bn[áa]usea", r"terap[êe]utic",
        r"anti-?inflamat", r"broncodilat", r"\bapetite\b", r"eleva (o )?humor", r"\bc[âa]ncer\b",
        # condições de saúde soltas: agora que os efeitos relatados passam, "bom para ansiedade" não pode.
        # A "ansiedade" do cultivador ("não colher cedo por ansiedade", "ansiedade com o prazo") fica.
        r"anti-?(ans|anx|depress|estress|em[ée]tic|convuls|tumor|c[âa]ncer)", r"\btdah\b", r"\bdepress[ãa]o\b", r"(para|contra|alivi\w*|trat\w*|ajud\w* (com|na|a)) (a )?ansiedade", r"\bdor(es)?\b", r"al[íi]vio d[ae]",
        r"c[óo]licas?", r"espasmos?", r"usos relatados", r"(atrativo|potencial|perfil|valor|cepa|strain|cultivar|gen[ée]tica|variedade) medicina",
    ]),
    re.IGNORECASE,
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ\[\*\"“(])")
# Marcador de lista e rótulo em negrito ("- **Efeitos reportados:** "): o "*" só conta como
# marcador seguido de espaço, para não engolir o "**" que abre o rótulo.
_MARKER = re.compile(r"^(?:[\s>\-+\d.]|\*(?=\s))*")
_PREFIX = re.compile(_MARKER.pattern + r"(\*\*[^*]+:\*\*\s*|\*\*[^*]+\*\*\s*:\s*)?")
_LABEL_ONLY = re.compile(r"^[\s>*\-+\d.]*(\*\*[^*]+\*\*\s*:?)?\s*:?\s*$")
# Referência solta que sobra quando a frase saiu: "(Cap. 1, p. 16)", "(Fonte: ...)"
_CITATION = re.compile(r"\((cap\.|caps?\b|p\.|pp\.|fig\.|tabela|fonte|ver\b|conforme\b)[^)]*\)", re.IGNORECASE)
# Rótulo que abre uma lista: "**Riscos:**", "Exemplos:"
_LIST_LABEL = re.compile(r"^[\s>*\-+]*(\*\*[^*]{1,60}:?\*\*|[^.!?|\[]{1,60}):\s*(\*\*)?\s*$")


def _has_substance(line: str) -> bool:
    """Sobrou texto de verdade, não só marcador, rótulo ou referência?"""
    return len(re.findall(r"[^\W\d_]{2,}", _CITATION.sub("", line))) >= 2


def is_blocked_path(rel_path: str) -> bool:
    return any(blocked in rel_path for blocked in BLOCKED_PATHS)


def is_blocked_text(text: str) -> bool:
    return bool(_BLOCKED_TEXT.search(text))


def is_blocked_tag(tag: str) -> bool:
    return is_blocked_text(tag) or bool(re.search(r"concentrad|extra[çc]|medicinal|loja", tag, re.IGNORECASE))


def _clean_line(line: str) -> str | None:
    """A linha sem as frases bloqueadas; None se não sobrar nada útil."""
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
        prefix = _PREFIX.match(parts[0]).group(0)
        if is_blocked_text(prefix):  # o próprio rótulo é bloqueado ("**Usos medicinais:**"): fica só o marcador
            prefix = _MARKER.match(parts[0]).group(0)
        cleaned = prefix + cleaned
    if cleaned.count("**") % 2:
        cleaned = cleaned.replace("**", "", 1)  # o negrito abria na frase que saiu
    if _LABEL_ONLY.match(cleaned) or not _has_substance(cleaned):
        return None
    return cleaned


def _drop_empty_blocks(lines: list[str]) -> list[str]:
    """Tira o que o corte deixou vazio: título sem conteúdo até o próximo título do
    mesmo nível (ou acima) e rótulo de lista sem itens antes de um título ou do fim."""
    out: list[str] = []
    for i, line in enumerate(lines):
        rest = (later for later in lines[i + 1:] if later.strip())
        following = next(rest, None)
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            section_empty = True
            for later in lines[i + 1:]:
                later_heading = _HEADING.match(later)
                if later_heading:
                    if len(later_heading.group(1)) <= level:
                        break
                    continue
                if later.strip():
                    section_empty = False
                    break
            if section_empty:
                continue
        elif _LIST_LABEL.match(line) and (following is None or _HEADING.match(following)):
            continue
        out.append(line)
    return out


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
    result = "\n".join(_drop_empty_blocks(_drop_empty_tables(out)))
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def sanitize_text(text: str | None) -> str | None:
    """Versão para um parágrafo solto (resumo da strain): só corta frases."""
    if text is None:
        return None
    kept = [s for s in _SENTENCE_SPLIT.split(text) if not is_blocked_text(s)]
    return " ".join(kept).strip() or None
