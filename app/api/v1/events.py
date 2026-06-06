"""
Rotas de eventos acessadas via prefixo /plants (mantidas por compatibilidade).
A lógica principal agora está em app/api/v1/plants.py — este módulo permanece
para não quebrar imports eventuais, mas o router não é mais registrado separadamente.
"""
# Este arquivo não registra mais um router próprio. As rotas de eventos
# foram consolidadas dentro de app/api/v1/plants.py para refletir a nova
# hierarquia User → Plant → Events.
