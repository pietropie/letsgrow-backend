# Regressões de privacidade e diário

Execute a partir de `letsgrow-backend`, com as dependências do backend instaladas:

```powershell
python -m unittest discover -s tests -v
```

Os testes montam somente os routers necessários, substituem autenticação por contas sintéticas e executam as consultas SQL reais em SQLite em memória. Armazenamento e efeitos do Bobby são simulados. Não inicializam o servidor completo nem conectam a PostgreSQL, MinIO, Redis ou IA.

São cobertos acesso entre contas, filtros combinados, arquivos anexados, formatos de avatar, chaves ambíguas, registros antigos, paginação, intervalo com timezone e histórico arquivado. A autorização real por token e as migrações PostgreSQL precisam de validação de integração separada.

No ambiente Codex/Windows, a criação do socket interno do event loop Python ficou bloqueada no sandbox. Os testes passaram fora dessa restrição, sem acesso aos serviços do projeto. O ambiente local `.venv-test` está ignorado pelo Git.

`test_grow_measurements.py` cobre origem e horário por indicador, leituras parciais, dados futuros/não finitos, isolamento entre ambientes e consistência com o resumo da planta. `test_plant_identity.py` cobre cadastro mínimo, compatibilidade legada, edição, identidade no diário e rejeição de dados inválidos/ambiente de outra conta. Total atual: 26 testes. A seleção SQL foi validada em SQLite; PostgreSQL continua exigindo integração separada.

`test_sem_api.py` (quarta entrega) cobre os recursos das telas que eram "Sem API": registro manual do ambiente e sua entrada no resolvedor de medições; evidências citadas pelos alertas do Bobby (sem expor registros de outra conta); feedback e memória editável (limite, isolamento, bloqueio de injeção, entrada no prompt); preferências de notificação (validação, horário local, silêncio, prévia discreta, "somente no app" também para envios do admin); créditos (desligados por padrão, concessão mensal única, cotação → reserva → captura, estorno em falha técnica, saldo insuficiente, expiração, reserva esquecida, compra sem provedor = 503, webhook idempotente, limite mensal quando os créditos não o substituem); cópia dos dados (somente dados e fotos da própria conta, e-mail sem link, download só do dono, intervalo de 24 h); exclusão (carência do admin, cancelamento, remoção completa, compra sem vínculo pessoal, falha de armazenamento preserva os dados); suporte (caixa vazia, encaminhamento com `reply_to`, nova tentativa, limite por hora) e rotas do admin (token, configurações, custos, pacotes, ajustes, listas). As chaves estrangeiras do SQLite ficam ligadas nesse arquivo para exercitar as cascatas. Total atual: 56 testes.

`test_grow_detail.py` roda `GET /grows/{id}` com uma `AsyncSession` real (`aiosqlite`), porque o adaptador síncrono dos outros testes não reproduz o `MissingGreenlet` de relações lazy lidas fora do greenlet (painel do ambiente com HTTP 500 em produção). Total atual: 63 testes.

O ambiente `.venv-test` precisou de `aiosqlite`, `alembic`, `apscheduler`, `resend`, `redis`, `tzdata`, `google-auth` e `python-multipart` (instalados com `py -3.12 -m pip install --target .venv-test/Lib/site-packages --no-deps ...`).

`test_plant_phase.py` (QA 2026-10-18) cobre a fase pelos registros FASE (germinação, muda, vega, flip a partir de qualquer fase anterior, colheita), a regra de não voltar a fase com registro antigo, a correção manual pelo PATCH, o registro múltiplo (`POST /diary`) e a data da próxima renovação da cota do Bobby. `test_sem_api.py` passou a checar `GET /account/deletion-policy`. Total atual: 69 testes.
