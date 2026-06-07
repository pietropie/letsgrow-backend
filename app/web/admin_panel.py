"""
Painel admin mínimo para trocar o provedor/modelo de IA em runtime.

Página HTML estática (sem build step, sem framework) servida diretamente pelo
FastAPI em GET /admin/ai-panel. Toda a lógica roda no navegador via fetch
contra /api/v1/admin/ai-config, autenticando com o header X-Admin-Token (ver
app/api/v1/admin.py e app/config.py:ADMIN_TOKEN).

O token é guardado em sessionStorage (não localStorage) — some ao fechar a
aba, então cada sessão precisa digitar de novo. Isso é proposital: é um painel
de emergência, não algo que fica logado o tempo todo.
"""

AI_PANEL_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Let's Grow — Painel de IA</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #14181b; color: #e7ece9; margin: 0; padding: 24px;
    display: flex; justify-content: center;
  }
  main { width: 100%; max-width: 640px; }
  h1 { font-size: 1.4rem; margin-bottom: 4px; }
  p.sub { color: #9aa6a1; margin-top: 0; font-size: 0.9rem; }
  section {
    background: #1d2327; border: 1px solid #2c3338; border-radius: 10px;
    padding: 20px; margin-bottom: 16px;
  }
  label { display: block; font-size: 0.85rem; color: #b7c1bd; margin: 12px 0 4px; }
  input, select {
    width: 100%; padding: 9px 10px; border-radius: 6px; border: 1px solid #3a4248;
    background: #111517; color: #e7ece9; font-size: 0.95rem;
  }
  small { color: #828d89; display: block; margin-top: 4px; font-size: 0.78rem; }
  button {
    margin-top: 16px; padding: 10px 18px; border-radius: 6px; border: none;
    background: #4f9d6e; color: #08120c; font-weight: 600; cursor: pointer; font-size: 0.95rem;
  }
  button.secondary { background: #2c3338; color: #e7ece9; margin-left: 8px; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .row { display: flex; gap: 12px; }
  .row > div { flex: 1; }
  #status { margin-top: 14px; font-size: 0.9rem; white-space: pre-wrap; }
  #status.ok { color: #7fd99a; }
  #status.err { color: #f08a7a; }
  #status.warn { color: #f0c97a; }
  code { background: #111517; padding: 1px 6px; border-radius: 4px; font-size: 0.85em; }
  #app { display: none; }
  .meta { font-size: 0.8rem; color: #828d89; margin-top: 10px; }
</style>
</head>
<body>
<main>
  <h1>🌱 Painel de IA — Let's Grow</h1>
  <p class="sub">Troca o provedor/modelo de chat e de embedding em runtime, sem redeploy.</p>

  <section id="login">
    <label for="token">Token de admin</label>
    <input id="token" type="password" placeholder="Cole o ADMIN_TOKEN configurado no servidor" autocomplete="off" />
    <small>Guardado só nesta aba (sessionStorage) — some ao fechar o navegador.</small>
    <button id="btn-login">Entrar</button>
    <div id="login-status"></div>
  </section>

  <div id="app">
    <section>
      <h2 style="margin-top:0; font-size:1.05rem;">Chat (modelo que responde no app)</h2>
      <p class="sub" style="margin-bottom:0;">Seguro trocar a qualquer momento — vale em até ~60s.</p>
      <div class="row">
        <div>
          <label for="provider">Provedor</label>
          <select id="provider"></select>
        </div>
        <div>
          <label for="temperature">Temperatura</label>
          <input id="temperature" type="number" step="0.1" min="0" max="2" />
        </div>
      </div>
      <label for="chat_model">Modelo</label>
      <input id="chat_model" placeholder="ex.: gemini-2.5-flash, claude-haiku-4-5, gpt-5-mini, deepseek-chat, glm-5" />
      <small>Use o nome exato do modelo conforme a documentação do provedor escolhido.</small>
    </section>

    <section>
      <h2 style="margin-top:0; font-size:1.05rem;">Embeddings (busca da wiki / RAG)</h2>
      <p class="sub" style="margin-bottom:0; color:#f0c97a;">
        ⚠️ Trocar provedor/modelo/dimensão aqui exige reindexar a wiki depois —
        vetores antigos e novos não são comparáveis entre si.
      </p>
      <div class="row">
        <div>
          <label for="embedding_provider">Provedor</label>
          <select id="embedding_provider"></select>
        </div>
        <div>
          <label for="embedding_dimensions">Dimensões</label>
          <input id="embedding_dimensions" type="number" min="1" />
        </div>
      </div>
      <label for="embedding_model">Modelo</label>
      <input id="embedding_model" placeholder="ex.: models/gemini-embedding-001" />
      <small>A coluna do banco está fixada em 768 dimensões — mude as "Dimensões" só se souber o que está fazendo (exige migration).</small>
    </section>

    <section>
      <label for="updated_by">Seu identificador (fica registrado na alteração)</label>
      <input id="updated_by" placeholder="ex.: pietro" />
      <button id="btn-save">Salvar</button>
      <button id="btn-reload" class="secondary">Recarregar</button>
      <div id="status"></div>
      <div class="meta" id="meta"></div>
    </section>
  </div>
</main>

<script>
const API = "/api/v1/admin/ai-config";
const $ = (id) => document.getElementById(id);

function token() { return sessionStorage.getItem("ai_panel_token") || ""; }
function authHeaders() { return { "X-Admin-Token": token(), "Content-Type": "application/json" }; }

function setStatus(el, msg, cls) {
  el.textContent = msg;
  el.className = cls || "";
}

async function loadOptions() {
  const res = await fetch(API + "/options", { headers: authHeaders() });
  if (!res.ok) throw new Error("Falha ao carregar opções de provedor (" + res.status + ")");
  const data = await res.json();
  fillSelect($("provider"), data.chat_providers);
  fillSelect($("embedding_provider"), data.embedding_providers);
}

function fillSelect(select, options) {
  select.innerHTML = "";
  for (const opt of options) {
    const el = document.createElement("option");
    el.value = opt;
    el.textContent = opt;
    select.appendChild(el);
  }
}

async function loadConfig() {
  const res = await fetch(API, { headers: authHeaders() });
  if (res.status === 403) throw new Error("forbidden");
  if (!res.ok) throw new Error("Falha ao carregar configuração (" + res.status + ")");
  const c = await res.json();
  $("provider").value = c.provider;
  $("chat_model").value = c.chat_model;
  $("temperature").value = c.temperature;
  $("embedding_provider").value = c.embedding_provider;
  $("embedding_model").value = c.embedding_model;
  $("embedding_dimensions").value = c.embedding_dimensions;
  $("updated_by").value = c.updated_by || "";
  $("meta").textContent = "Última alteração: " + new Date(c.updated_at).toLocaleString("pt-BR") +
    (c.updated_by ? " — por " + c.updated_by : "");
}

async function save() {
  const status = $("status");
  $("btn-save").disabled = true;
  setStatus(status, "Salvando…");
  try {
    const body = {
      provider: $("provider").value,
      chat_model: $("chat_model").value.trim(),
      temperature: parseFloat($("temperature").value),
      embedding_provider: $("embedding_provider").value,
      embedding_model: $("embedding_model").value.trim(),
      embedding_dimensions: parseInt($("embedding_dimensions").value, 10),
      updated_by: $("updated_by").value.trim() || null,
    };
    const res = await fetch(API, { method: "PUT", headers: authHeaders(), body: JSON.stringify(body) });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || ("Erro ao salvar (" + res.status + ")"));
    }
    const c = await res.json();
    $("meta").textContent = "Última alteração: " + new Date(c.updated_at).toLocaleString("pt-BR") +
      (c.updated_by ? " — por " + c.updated_by : "");
    if (c.reindex_required) {
      setStatus(status,
        "✓ Salvo! Mas você mudou o embedding — rode no servidor para reindexar a wiki:\\n" +
        "python scripts/index_brain.py --wiki-path /caminho/da/wiki --clear",
        "warn");
    } else {
      setStatus(status, "✓ Salvo. Vale em até ~60s (cache do servidor).", "ok");
    }
  } catch (e) {
    setStatus(status, "✗ " + e.message, "err");
  } finally {
    $("btn-save").disabled = false;
  }
}

async function enter() {
  const t = $("token").value.trim();
  if (!t) return;
  sessionStorage.setItem("ai_panel_token", t);
  const loginStatus = $("login-status");
  setStatus(loginStatus, "Verificando…");
  try {
    await loadOptions();
    await loadConfig();
    $("login").style.display = "none";
    $("app").style.display = "block";
  } catch (e) {
    sessionStorage.removeItem("ai_panel_token");
    setStatus(loginStatus, e.message === "forbidden" ? "✗ Token inválido." : "✗ " + e.message, "err");
  }
}

$("btn-login").addEventListener("click", enter);
$("token").addEventListener("keydown", (e) => { if (e.key === "Enter") enter(); });
$("btn-save").addEventListener("click", save);
$("btn-reload").addEventListener("click", () => loadConfig().catch((e) => setStatus($("status"), "✗ " + e.message, "err")));

// Se já tiver token guardado nesta aba, tenta entrar direto.
if (token()) {
  $("token").value = token();
  enter();
}
</script>
</body>
</html>
"""
