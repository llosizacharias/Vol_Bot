# Vol Trading Bot v3 🤖📈

Bot de volatilidade para Binance Spot — 24/7, sem alavancagem.

## O que há de novo na v3

| Feature | v1 | v2 | v3 ✅ |
|---|---|---|---|
| Filtro EMA50 > EMA200 | ❌ | ✅ | ✅ |
| Score composto (vol × momentum) | ❌ | ✅ | ✅ |
| Risco dinâmico por saldo | ✅ | ❌ | ✅ |
| Stop 2× ATR / TP 3× ATR | ✅ | 3×/4× | ✅ |
| Trailing stop 1.5× ATR | ✅ | ❌ | ✅ |
| Cooldown entre trades | ✅ | ✅ | ✅ |
| NpEncoder JSON fix | parcial | ✅ | ✅ |

## Estratégia

```
A cada hora:
  → Varre todos os pares USDT com volume > $5M
  → Filtra apenas tendência de alta (EMA50 > EMA200)
  → Calcula score = ATR% × (1 + MACD_h)
  → Elege o ativo com maior score

A cada 60s (sem posição):
  → Checa 6 condições de entrada simultaneamente:
    1. EMA50 > EMA200  (tendência)
    2. RSI 40–65       (momentum sem sobrecompra)
    3. MACD_h > 0      (momento positivo)
    4. MACD crossover  (sinal recente)
    5. Close > BB_mid  (preço acima da média)
    6. Volume ≥ 1.2×   (confirmação de força)

Em posição:
  → Stop Loss  = entrada - 2× ATR
  → Take Profit= entrada + 3× ATR → trailing ativa aqui
  → Trailing   = pico - 1.5× ATR (sobe com o preço)
  → Saída por indicador após 2h mínimo em posição
```

## Risco dinâmico

| Saldo | % por trade | Valor |
|---|---|---|
| $0 – $200 | 20% | ~$16–$40 |
| $200 – $500 | 10% | ~$20–$50 |
| $500 – $2000 | 5% | ~$25–$100 |
| $2000+ | 5% | ~$100+ |
| Teto absoluto | — | $500 |

## Setup

```bash
# 1. Clone
git clone https://github.com/SEU_USUARIO/vol_bot.git
cd vol_bot

# 2. Instalar dependências
pip3 install -r requirements.txt

# 3. Configurar credenciais
cp .env.example .env
nano .env

# 4. Rodar
python3 vol_bot.py

# 5. Em background (produção)
tmux new-session -d -s vol_bot 'cd ~/vol_bot && python3 vol_bot.py'

# 6. Auto-restart no reboot
crontab -e
# Adicionar:
@reboot cd ~/vol_bot && tmux new-session -d -s vol_bot 'python3 vol_bot.py'
```

## Monitoramento

```bash
# Ver bot rodando
tmux attach -t vol_bot
# Sair sem parar: Ctrl+B → D

# Logs em tempo real
tail -f logs/vol_bot_$(date +%Y-%m).log

# Estado atual
cat vol_state.json

# Parar o bot
tmux kill-session -t vol_bot
```

## Estrutura

```
vol_bot/
├── vol_bot.py          ← Bot principal
├── requirements.txt
├── .env                ← Suas chaves (não commitar!)
├── .env.example        ← Template
├── .gitignore
├── vol_state.json      ← Auto-criado: estado da posição
└── logs/
    └── vol_bot_YYYY-MM.log
```
