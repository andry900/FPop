# FPop eBay Sold Scraper

Progetto Python per estrarre prezzi dei Funko Pop dagli annunci venduti su eBay, con filtri per:

- annunci danneggiati/rovinati;
- annunci non pertinenti rispetto alla query;
- set/lotti multipli (resta solo il prezzo del pezzo singolo).

Lo script calcola la media sui primi 5 annunci validi (configurabile).

## Requisiti

- Python 3.10+
- Accesso web a eBay

## Setup

```bash
cd FPop
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Configurazione prodotti

Modifica [products.json](products.json) con la tua lista di URL eBay venduti.

Esempio:

```json
[
  {
    "name": "L Cake Funko Pop",
    "url": "https://www.ebay.it/sch/i.html?_nkw=L+cake+funko+pop&_sacat=0&_from=R40&LH_PrefLoc=1&LH_Sold=1&rt=nc",
    "quantity": 1
  }
]
```

Campi:

- name: nome libero del prodotto.
- url: link pagina eBay con filtro venduti.
- quantity (opzionale): quantita da moltiplicare per il totale.

## Esecuzione

Modalita browser visibile:

```bash
python scraper.py
```

Modalita headless:

```bash
python scraper.py --headless
```

Media su numero diverso di vendite valide:

```bash
python scraper.py --max-valid-sales 5
```

Parole vietate personalizzate:

```bash
python scraper.py --banned danneggiato rovinato rotto damaged broken
```

Output JSON personalizzato:

```bash
python scraper.py --output-json risultati_fpop.json
```

## Logica filtro

Per ogni riga venduta:

1. legge titolo e prezzo;
2. esclude se contiene parole vietate;
3. esclude se sembra set/lotto/multipack;
4. esclude se non abbastanza coerente con i token della query _nkw;
5. prende i primi N validi (default 5) e calcola la media.

## Output

Lo script salva [results.json](results.json) con:

- dettaglio per prodotto;
- vendite usate per la media (titolo + prezzo);
- media e subtotale per quantita;
- somma finale.
