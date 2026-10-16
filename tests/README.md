# Regressões de privacidade e diário

Execute a partir de `letsgrow-backend`, com as dependências do backend instaladas:

```powershell
python -m unittest discover -s tests -v
```

Os testes montam somente os routers necessários, substituem autenticação por contas sintéticas e executam as consultas SQL reais em SQLite em memória. Armazenamento e efeitos do Bobby são simulados. Não inicializam o servidor completo nem conectam a PostgreSQL, MinIO, Redis ou IA.

São cobertos acesso entre contas, filtros combinados, arquivos anexados, formatos de avatar, chaves ambíguas, registros antigos, paginação, intervalo com timezone e histórico arquivado. A autorização real por token e as migrações PostgreSQL precisam de validação de integração separada.

No ambiente Codex/Windows, a criação do socket interno do event loop Python ficou bloqueada no sandbox. Os testes passaram fora dessa restrição, sem acesso aos serviços do projeto. O ambiente local `.venv-test` está ignorado pelo Git.

`test_grow_measurements.py` cobre origem e horário por indicador, leituras parciais, dados futuros/não finitos, isolamento entre ambientes e consistência com o resumo da planta. `test_plant_identity.py` cobre cadastro mínimo, compatibilidade legada, edição, identidade no diário e rejeição de dados inválidos/ambiente de outra conta. Total atual: 26 testes. A seleção SQL foi validada em SQLite; PostgreSQL continua exigindo integração separada.
