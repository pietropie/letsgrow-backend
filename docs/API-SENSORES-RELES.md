# API de sensores, faixas ideais e relés do Hub

Este é o contrato que o app e o site consomem nas telas do item 28 do UX: 28 (sensores), 28b (relés pelo app) e 28c (4 posições por Hub). As telas estão no Figma `ZnB6jeHSKNsfVFOCocb9IX`, seção 2444:24432.

O contrato MQTT com o Hub está em `letsgrow-firmware/hub/RELAYS.md`.

Todas as rotas ficam em `/api/v1` e exigem login. Hub ou ambiente de outra conta respondem 404.

**Nenhum texto que a API devolve para a tela cita placa, MAC ou modelo de microcontrolador.** Nomes internos como `esp32_mac` continuam nos campos.

---

## 1. Dispositivos

### `GET /iot/devices` e `GET /iot/devices/{id}`

Cada dispositivo traz, além dos campos que já existiam:

| Campo | O que é |
|---|---|
| `sensor_kind` | `air`, `soil`, `light`, `solution`, `relay` ou `hub`. É `null` quando ainda não se sabe o tipo. Vem do módulo que o firmware informa. |
| `kind_label` | nome curto: "Ar", "Solo", "Luz", "Solução", "Relé" ou "Hub" |
| `description` | uma frase dizendo o que o sensor mede e onde fica |
| `icon` | chave do ícone: `temperature`, `watering`, `light`, `fert`, `exhaust` ou `hub` |
| `photo_url` | foto do **modelo** do sensor. Quando é `null`, o app mostra o ícone (é o fallback). |
| `mains_voltage` | só para o Hub: `127`, `220` ou `null`. `null` conta como 127 nos avisos de carga. |

O satélite recém-descoberto recebe um nome provisório pelo tipo, por exemplo "Sensor de solo", e não mais o endereço.

### `PATCH /iot/devices/{id}`

O PATCH ganhou dois campos:
- `mains_voltage`: aceita `127`, `220` ou `null`, e só no Hub. Em outro dispositivo, responde 422 `mains_voltage_hub_only`.
- `sensor_kind`: corrige o tipo de um aparelho antigo.

### Leituras

`GET /sensors/{id}/latest` e `GET /sensors/{id}/readings` passam a trazer **`ppm`**, calculado do EC na **escala 500** (`ppm = ec_ms_cm × 500`).

---

## 2. Tela de Sensores: `GET /iot/grows/{grow_id}/sensors`

Uma chamada monta a tela inteira (SN1–SN4): o Hub, um cartão por tipo medido, a avaliação contra a faixa ideal e o mini-histórico.

```json
{
  "grow_id": "…", "phase": "flower", "phase_basis": "plants", "light_on": true,
  "hub": {"id": "…", "name": "Hub 3EE0", "sensor_kind": "hub", "kind_label": "Hub",
          "tag": "A base · fica ligada no Wi-Fi", "description": "…", "icon": "hub", "photo_url": null,
          "online": true, "last_seen_at": "…", "satellites_total": 4, "satellites_online": 4, "clock": "ntp"},
  "cards": [{
    "device_id": "…", "card_id": "<device_id>:soil", "name": "Solo vaso 1", "source": "satellite",
    "sensor_kind": "soil", "kind_label": "Solo", "tag": "Umidade do substrato",
    "description": "Fica espetado no substrato…", "icon": "watering", "photo_url": null,
    "placement": {"plant_id": "…", "label": "Vaso 1 · Aurora"},
    "status": "live", "is_online": true, "last_reading_at": "…",
    "metrics": [{"metric": "soil_moisture_pct", "label": "Umidade do substrato", "unit": "%", "decimals": 0,
                 "computed": false, "note": null, "value": 21,
                 "range": {"min": 35, "max": 60, "source": "phase", "phase": "flower", "needs_review": true,
                           "reference": "…"},
                 "state": "out", "out_of_range_since": "2026-11-25T14:00:00Z"}],
    "worst_state": "out",
    "history": {"metric": "soil_moisture_pct", "window": "3d", "bucket_minutes": 240,
                "buckets": [{"start": "…", "avg": 48.2, "min": 45, "max": 51, "n": 12, "state": "ok"}],
                "min": 20, "max": 51}
  }],
  "out_of_range_count": 1, "generated_at": "…"
}
```

**`hub`**
- É `null` quando o ambiente não tem Hub (tela AM2, "Adicionar Hub").
- `online` só é true com o status em `online:true` **e** um heartbeat de menos de 150 s.

**Cartões**
- O **Hub também mede** ar, luz, solo e solução. As medidas dele viram cartões com `source: "hub"`, um por tipo presente na última leitura.
- Satélite sem tipo conhecido vira um cartão por tipo que aparece na leitura.

**`status` do cartão**

| Valor | Significado |
|---|---|
| `live` | leitura de menos de 30 min |
| `no_signal` | o satélite parou de mandar dados (SN2c) |
| `stale` | o Hub está offline e o cartão mostra a última leitura esmaecida (SN2b) |
| `waiting` | o satélite ainda não mandou a primeira leitura (SN2d) |

**`metrics[]`**
- Já vêm na ordem da tela. `computed: true` marca valor calculado (VPD e PPM), que leva o marcador "CALCULADO".
- `state` pode ser:
  - `ok`: na faixa;
  - `near`: perto do limite, até 10 % da largura da faixa de uma das bordas;
  - `out`: fora da faixa;
  - `none`: sem leitura, sem faixa ou leitura antiga.
- `out_of_range_since` diz desde quando a medida está fora da faixa sem voltar a ela (olha até 7 dias). Serve para a frase "Abaixo da faixa há 5 horas".
- **Luz:** com a luz apagada pela programação (`light_on: false`), o lux vem sem faixa e com `state: "none"`.

**`worst_state`:** pior estado entre as medidas. A lista já vem ordenada **fora da faixa primeiro**, depois perto do limite, depois na faixa, e por nome dentro de cada grupo.

**`history`:** mini-histórico da medida principal do tipo. A última barra é a mais recente, e `state` de cada barra pinta de rosa os trechos fora da faixa.

| Tipo | Medida | Janela |
|---|---|---|
| ar | temperatura | 24 h, 24 barras de 1 h |
| luz | lux | 24 h |
| solo | umidade do substrato | 3 dias, 18 barras de 4 h |
| solução | PPM | 7 dias, 14 barras de 12 h |

`min`/`max` são o "MÍN · MÁX" da janela.

**Filtros "fora da faixa" e "por vaso":** são feitos no app, com `worst_state` e `placement.plant_id`.

### Histórico de qualquer medida: `GET /sensors/{device_id}/history?metric=…&window=24h|3d|7d`

- `metric` pode ser `temp_air`, `humidity_air`, `vpd_kpa`, `lux`, `soil_moisture_pct`, `ppm`, `ec_ms_cm`, `co2_ppm`, `ph_solution` ou `temp_root`.
- A resposta tem o mesmo formato de `history`, com `device_id`, `unit` e `range`.

---

## 3. Faixas ideais

O app sugere a faixa pela fase da planta, e o usuário pode ajustar no ambiente. A faixa ajustada vale para **todas as fases** e tem prioridade sobre a sugerida.

- **Fase do ambiente:** é a da maioria das plantas ativas; no empate, vale a fase mais avançada.
- **Cartão ligado a uma planta** (por exemplo, o solo): usa a fase dessa planta.
- **`phase_basis`:**
  - `plants`: a fase veio das plantas;
  - `assumed`: nenhuma planta tem fase registrada, e vale a sugestão do vegetativo;
  - `none`: o ambiente não tem plantas.
- Colheita, secagem e cura não têm faixa sugerida.

### Endpoints

- `GET /grows/{grow_id}/ideal-ranges`
- `PUT /grows/{grow_id}/ideal-ranges/{metric}`, com corpo `{"min": 50, "max": 65}`
- `DELETE /grows/{grow_id}/ideal-ranges/{metric}`, que volta à faixa sugerida

`metric` ajustável: `temp_air`, `humidity_air`, `vpd_kpa`, `lux`, `soil_moisture_pct` ou `ppm`.

**EC acompanha o PPM** (EC = PPM / 500) e não é ajustável à parte; a lista mostra o EC com `adjustable: false`.

Erros de validação (422): `min ≥ max`, ou valor fora do aceito para a medida. Os limites aceitos são:

| Medida | Mínimo | Máximo |
|---|---|---|
| temperatura | 0 | 50 |
| umidade | 0 | 100 |
| VPD | 0 | 4 |
| lux | 0 | 200 000 |
| umidade do substrato | 0 | 100 |
| PPM | 0 | 5000 |

Resposta:

```json
{"grow_id": "…", "phase": "flower", "phase_basis": "plants",
 "ranges": [{"metric": "ppm", "label": "PPM", "unit": "ppm", "min": 700, "max": 1000, "source": "custom",
             "needs_review": false, "adjustable": true,
             "suggested": {"min": 800, "max": 1100, "source": "phase", "phase": "flower",
                           "needs_review": true, "reference": "…"}}]}
```

### Faixas sugeridas por fase

O grupo "muda" inclui a germinação. **(r)** marca valor com `needs_review: true`, que ainda precisa de revisão.

| Medida | Muda | Vega | Flora | Fim da flora |
|---|---|---|---|---|
| Temperatura (°C) | 24–26 | 24–28 | 22–26 | 22–26 |
| Umidade (%) | 75–85 (r) | 55–70 | 45–60 | 40–50 |
| VPD (kPa) | 0,4–0,7 | 0,8–1,2 | 1,2–1,6 | 1,2–1,6 |
| Luz (lx) | 14 000–21 000 (r) | 42 000–63 000 (r) | 63 000–84 000 (r) | 63 000–84 000 (r) |
| Umidade do substrato (%) | 35–60 (r) | 35–60 (r) | 35–60 (r) | 35–60 (r) |
| PPM (escala 500) | 100–300 (r) | 400–800 (r) | 800–1100 (r) | 600–900 (r) |

**Fontes:**
- **Temperatura, umidade e VPD:** wiki `concepts/vpd-deficit-pressao-vapor.md` (Cultlight; o fim da flora vem do GrowWeedEasy).
- **Umidade de muda:** fica marcada para revisão porque o mesmo wiki traz 60–70 % em outra fonte.
- **Luz:** é o PPFD do wiki convertido para lux (cerca de 70 lx por µmol/m²/s em LED branco). A conversão depende do espectro, e na flora a faixa passa do limite do sensor de luz, que é 65 535 lx.
- **Umidade do substrato:** é a proposta do desenho. O índice do sensor capacitivo não é umidade volumétrica.
- **PPM:** são valores usuais de cultivo; variam por fabricante de nutriente.

---

## 4. Relés do Hub

O Hub manda nos relés: a programação e a regra de umidade rodam nele, mesmo sem app e sem internet. O servidor faz quatro coisas:
- guarda o vínculo de cada posição;
- publica a configuração;
- repassa os comandos;
- registra o que o Hub confirma.

**A chave do app mostra só o estado confirmado pelo Hub.**

### `GET /iot/devices/{hub_id}/relays`

```json
{"hub_id": "…", "grow_id": "…", "online": true, "last_seen_at": "…",
 "synced": true, "rev": 3, "synced_rev": 3, "sync_error": null, "clock": "ntp",
 "mains_voltage": 127, "mains_voltage_set": false, "output_limit_w": 1000, "total_limit_w": 1200,
 "relays": [{"n": 1, "equip_kind": "light", "equip_key": "light", "equip_name": "LED quantum board 720 W",
             "kind_label": "Iluminação", "watts": 720, "mode": "schedule", "effective_mode": "schedule",
             "schedule": {"on": "06:00", "off": "00:00"}, "humidity": null, "failsafe": null,
             "state": 1, "override": false, "source": "schedule", "ts": "…", "confirmed": true,
             "schedule_lit": true, "next_change_at": "2026-11-26T03:00:00Z"}],
 "equipment": [{"kind": "fan", "key": "fan:3f2a9c1b", "name": "Ventiladores 25 W (2)", "kind_label": "Ventilação",
                "watts": 50, "n": 3, "hub_id": "…"}],
 "warnings": [{"code": "missing_watts", "n": 2}]}
```

**Posições e equipamentos**
- `relays` traz sempre as 4 posições. Posição livre tem `equip_kind: null`.
- `equipment` lista o que pode ir numa posição: a luz (`light`), o exaustor (`exhaust`) e cada ventilador (`fan:<id>`). Os nomes vêm do cadastro: tipo e potência, ou CFM no caso do exaustor.
- `n` e `hub_id` dizem onde o equipamento já está, contando **todos os Hubs do ambiente**. Esses itens aparecem esmaecidos com "NA POSIÇÃO 2".

**Estado**
- `state`/`ts`: último estado conhecido e desde quando.
- `confirmed: false` quando o estado ainda não veio do Hub. Exemplo: logo depois de trocar o equipamento, o estado fica `0` com `source: "rebind"`.
- `override: true`: um comando manual está pausando o modo automático.

**Programação**
- `schedule_lit` diz se a programação manda ligado agora, e `next_change_at` traz a próxima borda.
- Com isso o app monta "LIGADA · ATÉ 22:00" e decide a confirmação do SN7a: **só pede confirmação para desligar a luz quando `schedule_lit` for true**.
- **A luz em `schedule` segue o fotoperíodo do ambiente** (`photoperiod_on_at` + `photoperiod_light_minutes`). Mudar o fotoperíodo republica a configuração.
- Em 24/0, a luz vai como `schedule` 00:00–24:00: fica sempre ligada, sem depender do relógio do Hub, e `next_change_at` vem `null`.
- Sem fotoperíodo completo, `effective_mode` vira `manual`.

**Sincronização**
- `synced` fica false até o Hub confirmar o `rev` publicado.
- `sync_error` traz o `detail` do `config_ack` recusado (por exemplo `schedule`), ou `publish_failed` quando o broker estava fora do ar ao mudar o ambiente.

**Online:** a mesma regra do painel. Offline, as chaves ficam bloqueadas (SN6c).

**Avisos de carga** (`warnings`): avisam, mas não bloqueiam.

| `code` | Quando aparece | Frase para a tela |
|---|---|---|
| `over_output_limit` | a posição `n` passa de 1.000 W | "Acima do limite seguro desta saída. Use um contator ou um relé maior." |
| `over_total_limit` | a soma passa de 1.200 W em 127 V ou de 2.000 W em 220 V | "A soma passa do que uma tomada de 10 A aguenta." |
| `missing_watts` | o equipamento da posição `n` não tem potência cadastrada | "Potência não informada." |

### `PUT /iot/devices/{hub_id}/relays/{n}` (n = 1–4)

Campo ausente não muda nada.

```json
{"equip_kind": "fan", "equip_key": "fan:3f2a9c1b", "mode": "humidity",
 "humidity": {"on_above": 70, "off_below": 60, "min_on_s": 60, "min_off_s": 60},
 "schedule": {"on": "08:00", "off": "20:00"}, "failsafe": "on", "move": false}
```

**Vínculo**
- **Atribuir:** mande `equip_kind` e `equip_key`. Para a luz e o exaustor, `equip_key` pode faltar, porque a chave é o próprio tipo.
- **Liberar:** `{"equip_kind": null, "equip_key": null}`. O modo volta para `manual`.
- **Mover:** se o equipamento está em outra posição, responde **409** `{"detail": "equipment_in_use", "n": 2, "hub_id": "…"}`. Depois que o usuário confirmar (SN9b), repita com `"move": true`: a posição antiga fica livre.

**Desligar antes de trocar:** quando o equipamento de uma posição muda, o próprio Hub desliga a saída antes (evento `rebind`). **O app não precisa mandar "desligar" antes de liberar ou trocar.** Isso também cobre o SN10b.

**Modos**
- `manual`: sem regra automática.
- `schedule`:
  - na luz, usa o fotoperíodo do ambiente (422 `photoperiod_required` se não houver);
  - no exaustor e nos ventiladores, usa o próprio `schedule` (422 `schedule_required` se não houver).
- `humidity`: só para exaustor e ventiladores (422 `mode_not_allowed` na luz). Exige `humidity`, com `off_below < on_above` (422 `humidity_required` se não houver).

**Outros erros**
- 422 `equipment_not_found`: o equipamento não existe no ambiente.
- 422 `relay_free`: pediu um modo automático numa posição livre.

**Publicação**
- Grava e publica a configuração inteira (as 4 posições) em `relay/config`, com retain e o `rev` seguinte, e devolve o mesmo corpo do GET.
- Com o Hub offline também grava: ele recebe a configuração quando conectar.
- Broker fora do ar: **503** `broker_unavailable`, e nada muda.

### `POST /iot/devices/{hub_id}/relays/{n}/command`

**Corpo**
- Ligar: `{"state": 1}`.
- Desligar: `{"state": 0}`.
- Tempo opcional: `"duration_s"`, até 86400.
- Encerrar a pausa: `{"action": "resume"}`.

**Resposta:** **202** `{"req_id": "…", "status": "pending"}`.

**Erros**
- 409 `relay_free`: posição sem equipamento.
- 409 `hub_offline`.
- 503 `broker_unavailable`.

**Pausa do automático:** com o modo automático ligado, um comando manual vira pausa.
- No modo `schedule`, a pausa vale até a próxima borda da programação.
- No modo `humidity`, vale até a umidade pedir o mesmo estado, com limite de 2 h.

### `GET /iot/relay-commands/{req_id}`

O app consulta a cada cerca de 1 s.

```json
{"req_id": "…", "status": "confirmed", "relay": 1, "action": "set", "requested_state": 1,
 "state": 1, "override": true, "err": null, "created_at": "…", "resolved_at": "…"}
```

| `status` | Significado | Tela |
|---|---|---|
| `pending` | aguardando o Hub | "LIGANDO… AGUARDANDO O HUB" |
| `confirmed` | o Hub executou; `state` é o estado real | a chave muda |
| `failed` | o Hub recusou (`err`: `bad_relay` ou `bad_duration`) | erro |
| `timeout` | ainda pendente 10 s depois do pedido | "O HUB NÃO CONFIRMOU" (SN6b) |

O `timeout` é calculado na hora da consulta. Um ack que chega atrasado ainda atualiza o estado da posição.

### Histórico do ambiente

Toda mudança de estado confirmada pelo Hub entra no histórico do ambiente (`GET /grows/{id}/readings`) como registro de `kind: "relay"`, somente leitura. O texto diz quem mudou, por exemplo:
- "Luz (LED quantum board 720 W) ligada pela programação · relé 1";
- "Exaustor (…) ligado pela umidade (71 %) · relé 2";
- "Ventiladores (…) desligados pelo app · relé 3".

Eventos do Hub inteiro só entram quando importam para o usuário:
- `config_lost`: o Hub perdeu a configuração dos relés;
- `no_clock`: o Hub está sem hora, e a luz programada fica desligada.

### Equipamentos do ambiente

Mudanças em `POST` e `PATCH /grows/{id}` que afetam os relés:

**Ventiladores**
- Cada item de `fans` ganha um **`id` estável**, gerado pelo servidor: `{"id": "3f2a9c1b", "location": "copa", "watts": 25, "qty": 2, "on": true}`.
- **Ao editar a lista, o app precisa devolver o `id` de cada ventilador.** Item sem `id` conta como ventilador novo.
- `qty` (opcional, padrão 1) agrupa ventiladores iguais na mesma tomada. A potência da posição é `watts × qty`.

**Exaustor:** o campo novo `exhaust_watts` guarda a potência elétrica, em W, usada nos avisos de carga.

**Equipamento que sai do ambiente:** ventilador removido da lista, ou luz/exaustor que passa a `none`. A posição dele é liberada e a configuração é republicada; o Hub desliga a saída.

### Hub trocado de ambiente ou removido

Na reativação em outro ambiente, ou no `DELETE /iot/devices/{hub_id}`:
- as posições são apagadas;
- a configuração retida no tópico antigo é limpa.

---

## 5. Admin: foto por modelo de sensor

Proteção: header `X-Admin-Token`.

- `GET /admin/iot/sensor-models` lista os modelos.
- `PUT /admin/iot/sensor-models/{key}` com `{"name": "…", "sensor_kind": "soil", "photo_url": "https://…"}` cria ou altera um modelo. `photo_url: null` ou `""` apaga a foto.

**Chave do modelo:** é o `sensors_config.model` do aparelho. Sem ele, vale o módulo do firmware: `hub`, `ambiente`, `solo`, `ppfd`, `nutricao` ou `automacao`. Esses seis já vêm cadastrados, sem foto.

**Ordem de busca da foto:** primeiro a do modelo; depois a do módulo; sem as duas, `photo_url: null` e o app mostra o ícone.
