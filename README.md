# Zany — Economia, Loja e Minijogos (plugin YourBot)

Zip (tudo na raiz): __main__.py · requirements.txt · extensions/economy_shop.py · extensions/zany_games.py · data/

Prefixo: z!   (também funcionam os /slash commands). Ex.: z!ajuda, z!saldo, z!crash 500

Economia:  saldo(money) diario(daily) trabalhar(work) pagar(pay) ranking(rank/top) + moedas por conversar
Apostas:   apostar [cara|coroa] · slots · crash · mines [minas]
Loja:      loja(shop) comprar(buy) inventario(inv) usar(use)
Drops:     drops #canal [min] · dropagora · atividade on/off
Admin:     painel (painel público com botões) · adminloja (gestão completa)

## Se o bot "não responde"
1. Token: DISCORD_TOKEN / TOKEN / BOT_TOKEN ou config.json {"token": "..."}.
2. Comandos z! precisam de "Message Content Intent" ativo no Developer Portal.
3. Slash commands: convite com scope `applications.commands` (sincronizam sozinhos).
4. Cargos na loja: o cargo do bot tem de estar acima dos cargos vendidos.
