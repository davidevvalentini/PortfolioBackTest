# PortfolioBackTest

App locale per creare e gestire portafogli (ISIN + peso % + timeframe) e
ottenerne l'andamento storico e una performance summary, usando Yahoo
Finance come sorgente dati tramite `yfinance`.

Frontend (singolo `index.html`, Chart.js) e backend (`server.py`, solo
libreria standard + `yfinance`/`requests`) girano sullo stesso server
locale: niente CORS, niente chiavi API da configurare.

## Struttura del repo

```
PortfolioBackTest/
├── index.html               # frontend (UI, grafici, calcolo metriche)
├── server.py                # server locale + proxy dati (Yahoo, ECB, Frankfurter)
├── requirements.txt         # dipendenze Python
├── portafogli-preset.json   # portafogli di esempio da importare dall'app
├── .gitignore
├── LICENSE                  # MIT
└── README.md
```

`price_cache.sqlite3` viene creato automaticamente al primo avvio nella
cartella del progetto (cache dei prezzi + stato salvato dell'app). Non è
versionato: contiene i tuoi dati locali.

## Avvio

Requisiti: Python 3.9+.

```bash
git clone https://github.com/davidevvalentini/PortfolioBackTest.git
cd PortfolioBackTest

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python3 server.py
```

Poi apri **http://localhost:8765** nel browser.

> Il frontend carica Chart.js e i font da CDN: serve una connessione
> internet attiva.

## Come si usa

1. Crea un portafoglio dalla barra laterale.
2. Per ogni riga, scrivi l'ISIN (o il nome dello strumento) nella prima
   colonna: dopo una breve ricerca compare un elenco di ticker candidati
   trovati su Yahoo Finance — clicca quello giusto per compilare
   automaticamente ticker e nome. Se la ricerca non trova nulla, puoi
   sempre scrivere il ticker a mano nella seconda colonna.
3. Imposta il peso % di ogni riga (non serve che sommino esattamente a
   100: vengono normalizzati automaticamente al calcolo).
4. Scegli l'intervallo temporale (preset o date personalizzate) e il
   metodo di ribilanciamento (Buy & Hold oppure mensile/trimestrale/
   annuale).
5. Premi **Calcola**.

Ottieni: equity curve indicizzata a 100, grafico del drawdown,
importo iniziale/finale, rendimento totale, CAGR, volatilità annualizzata,
Sharpe ratio, Sortino ratio, max drawdown, anno migliore/peggiore e matrice
di correlazione tra gli strumenti.

I portafogli vengono salvati nel browser (localStorage) e nello stato
locale del server: **non sono sincronizzati** tra dispositivi diversi.
Usa **Esporta JSON** / **Importa JSON** nella barra laterale per salvare
o trasferire i tuoi portafogli come file, o per caricare quelli
predefiniti in `portafogli-preset.json`.

## API del server locale

| Endpoint | Descrizione |
|---|---|
| `GET /api/resolve?q=` | Ticker candidati per un ISIN o testo libero (Yahoo Finance) |
| `GET /api/history?ticker=&start=&end=` | Serie storica dei prezzi di chiusura (con cache SQLite) |
| `GET /api/classify` | Classificazione dello strumento |
| `GET /api/fx` | Cambi valutari storici (Frankfurter) |
| `GET /api/inflation` | Serie inflazione (HICP) |
| `GET /api/risk-free?currency=EUR` | Tasso privo di rischio aggiornato (BCE, solo EUR) |
| `GET/POST /api/state` | Lettura/salvataggio dello stato dell'app |

## Confronto con PortfolioVisualizer o altri tool

Se confronti i numeri con PortfolioVisualizer (o altri backtest tool),
tieni presente che piccole differenze sono normali e dipendono da:

- **Intervallo di date esatto**: un preset come "5A" qui calcola la data
  di 5 anni fa da oggi, non necessariamente lo stesso range usato altrove.
  Per un confronto preciso usa le date personalizzate.
- **Tasso privo di rischio**: qui il default è 0%, mentre altri tool
  spesso usano un tasso storico (es. T-Bill USA) per Sharpe e Sortino —
  imposta lo stesso valore per confrontare le due cifre.
- **Fonte dati**: Yahoo Finance vs altri provider possono avere prezzi
  leggermente diversi (specie per dividendi/rettifiche).
- **Metodo Sortino**: esistono più convenzioni per la downside deviation;
  qui si usa il tasso privo di rischio come soglia (MAR).

## Limiti da conoscere

- **Risoluzione ISIN → ticker**: si appoggia alla ricerca pubblica di
  Yahoo Finance, che funziona bene per la maggior parte di azioni ed
  ETF ma non è garantita al 100% (specie per fondi meno comuni o quotati
  su più borse). In quei casi, inserisci il ticker manualmente.
- **Valute**: se combini strumenti in valute diverse, i prezzi vengono
  convertiti nella valuta base del portafoglio usando i cambi storici
  di Frankfurter (BCE). Eventuali giorni senza cambio usano l'ultimo
  valore noto.
- **Giorni festivi/mercati diversi**: quando i titoli quotano su borse
  con calendari diversi (es. Milano vs New York), i prezzi mancanti nei
  giorni di chiusura di un mercato vengono "portati avanti" (ultimo
  prezzo noto) per allineare le serie.
- **Dati Yahoo Finance**: gratuiti ma non garantiti al 100% per
  affidabilità/continuità nel tempo.

## Disclaimer

Strumento a scopo informativo e di studio. I risultati storici non sono
indicativi di quelli futuri e non costituiscono consulenza finanziaria.

## Licenza

Rilasciato sotto licenza [MIT](LICENSE).
