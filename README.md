# Torreto

Torreto est un moteur de recherche intelligent pour site e-commerce.

Le projet fournit une API de recherche conversationnelle branchee sur Akeneo et Typesense. Le site hote peut integrer sa propre barre de recherche ou embarquer un composant dedie, puis appeler Torreto pour obtenir des resultats produits legers et exploitables.

Le site reste responsable de son rendu et de ses pages produit. Torreto retourne surtout les cles fonctionnelles (`sku`, `source_id`) et les informations minimales necessaires pour afficher une liste de resultats.

## Ce que fait Torreto

- ingestion catalogue depuis Akeneo
- full sync initial et delta sync par `updated_at`
- normalisation produit dans un modele interne
- indexation Typesense
- comprehension NLP via Groq si configure
- reranking via Cohere si configure
- memoire conversationnelle par `visitor_id + session_id`
- recherche par texte naturel, marque, categorie, attributs, SKU et reference exacte
- API HTTP consommable par n'importe quel site client
- frontend Next.js de demo pour valider les comportements

## Architecture

```text
Site e-commerce
  |
  | POST /api/v1/search
  v
Backend FastAPI
  |-- Query understanding: heuristiques + Groq optionnel
  |-- Retrieval: Typesense
  |-- Reranking: scoring metier + Cohere optionnel
  |-- Session: memoire en RAM par visitor_id + session_id
  |
  v
Resultats legers: sku, source_id, name, brand, categories, price, stock

Akeneo -> full sync / delta sync -> normalisation -> Typesense
```

## Structure du projet

```text
.
|-- backend
|   |-- app
|   |   |-- api
|   |   |-- core
|   |   |-- domain
|   |   `-- services
|   |-- data
|   `-- tests
|-- docs
|-- frontend
|-- scripts
|-- docker-compose.yml
`-- README.md
```

## Stack

- Backend : FastAPI
- Frontend demo : Next.js
- Search index : Typesense
- Source catalogue : Akeneo
- NLP : Groq optionnel
- Reranking : Cohere optionnel
- Redis/PostgreSQL : declares dans Docker Compose, mais pas encore requis dans le flux nominal

## Demarrage local

### 1. Configurer l'environnement

```bash
cp .env.example .env
```

Remplir au minimum :

```bash
CATALOG_MODE=akeneo
AKENEO_BASE_URL=https://stimplus.cloud.akeneo.com
AKENEO_USERNAME=...
AKENEO_PASSWORD=...
AKENEO_CLIENT_ID=...
AKENEO_CLIENT_SECRET=...
TYPESENSE_URL=http://typesense:8108
TYPESENSE_API_KEY=xyz
```

Pour activer les couches IA :

```bash
GROQ_API_KEY=...
COHERE_API_KEY=...
```

Ne jamais commit le fichier `.env`.

### 2. Lancer la stack

```bash
docker compose up --build
```

Services :

- frontend demo : `http://localhost:3000`
- backend API : `http://localhost:8000`
- Typesense : `http://localhost:8108`

### 3. Verifier l'etat

```bash
scripts/health.sh
```

Equivalent :

```bash
curl http://127.0.0.1:8000/health
```

Reponse attendue :

```json
{
  "status": "ok",
  "products": 217039,
  "catalog_mode": "akeneo",
  "catalog_source": "https://stimplus.cloud.akeneo.com",
  "typesense_status": "ok",
  "typesense_indexed_products": 217039
}
```

## Variables d'environnement

### Backend

| Variable | Role |
| --- | --- |
| `FRONTEND_ORIGIN` | Domaine autorise en CORS |
| `CATALOG_MODE` | `sample` ou `akeneo` |
| `CATALOG_SOURCE_PATH` | chemin du sample local |
| `CATALOG_SNAPSHOT_PATH` | snapshot runtime local |
| `SYNC_STATE_PATH` | checkpoint de sync |
| `AKENEO_BASE_URL` | URL Akeneo |
| `AKENEO_USERNAME` | utilisateur API Akeneo |
| `AKENEO_PASSWORD` | mot de passe API Akeneo |
| `AKENEO_CLIENT_ID` | client OAuth Akeneo |
| `AKENEO_CLIENT_SECRET` | secret OAuth Akeneo |
| `AKENEO_PREFERRED_LOCALE` | locale prioritaire, ex. `fr_FR` |
| `AKENEO_FALLBACK_LOCALE` | locale fallback, ex. `en_US` |
| `AKENEO_PAGE_LIMIT` | taille de page Akeneo |
| `AKENEO_MAX_PRODUCTS` | plafond optionnel d'import |
| `TYPESENSE_URL` | URL Typesense |
| `TYPESENSE_API_KEY` | cle API Typesense |
| `TYPESENSE_COLLECTION` | collection produits |
| `TYPESENSE_IMPORT_BATCH_SIZE` | taille des batches d'import |
| `TYPESENSE_CANDIDATE_MULTIPLIER` | largeur de retrieval |
| `GROQ_API_KEY` | active la comprehension LLM |
| `GROQ_MODEL` | modele Groq |
| `COHERE_API_KEY` | active le reranking Cohere |
| `COHERE_RERANK_MODEL` | modele Cohere |

### Frontend demo

| Variable | Role |
| --- | --- |
| `NEXT_PUBLIC_API_URL` | URL publique du backend |

## Ingestion catalogue

L'ingestion passe par l'endpoint `POST /api/v1/catalog/reindex`.

### Full sync

A utiliser :

- premiere initialisation
- changement du normaliseur catalogue
- reconstruction propre de Typesense
- correction d'une divergence d'index

Commande :

```bash
scripts/full-sync.sh
```

Equivalent :

```bash
curl -X POST http://127.0.0.1:8000/api/v1/catalog/reindex \
  -H 'Content-Type: application/json' \
  -d '{"source":"akeneo","sync_mode":"full","reset_index":true}'
```

Effet :

- reset de la collection Typesense
- relecture du catalogue Akeneo
- normalisation de tous les produits
- import complet dans Typesense
- mise a jour du checkpoint `last_akeneo_updated_at`

### Delta sync

A utiliser en exploitation reguliere.

Commande :

```bash
scripts/delta-sync.sh
```

Equivalent :

```bash
curl -X POST http://127.0.0.1:8000/api/v1/catalog/reindex \
  -H 'Content-Type: application/json' \
  -d '{"source":"akeneo","sync_mode":"delta"}'
```

Effet :

- relit uniquement les produits modifies depuis le dernier checkpoint Akeneo
- pousse un `upsert` cible dans Typesense
- avance le checkpoint

Limite actuelle :

- le delta gere les creations/modifications
- la suppression miroir Akeneo n'est pas encore geree automatiquement
- un full sync periodique reste conseille pour reconciler les suppressions

Frequence recommandee :

- delta toutes les 5 a 15 minutes selon la criticite catalogue
- full sync manuel ou planifie la nuit apres changement structurel

## Contrat API pour le site frontend

### Recherche

Endpoint :

```http
POST /api/v1/search
```

Payload minimal :

```json
{
  "query": "iphone",
  "limit": 6,
  "session_id": "session-123",
  "visitor_id": "visitor-abc",
  "reset_context": false,
  "previous_context": null
}
```

Champs :

| Champ | Obligatoire | Description |
| --- | --- | --- |
| `query` | oui | texte saisi par l'utilisateur |
| `limit` | non | nombre de resultats, de `1` a `60` |
| `session_id` | recommande | session de recherche courante |
| `visitor_id` | recommande | utilisateur anonyme ou connecte |
| `reset_context` | non | remet la memoire a zero |
| `previous_context` | recommande | dernier `structured_query` retourne par l'API |

Reponse :

```json
{
  "query": "iphone",
  "session_id": "session-123",
  "assistant_message": "Je peux affiner cette recherche...",
  "suggestion_chips": ["iphone 128 go", "iphone noir"],
  "structured_query": {
    "raw_query": "iphone",
    "normalized_text": "iphone smartphone apple",
    "keywords": ["iphone"],
    "filters": {
      "brand": ["Apple"],
      "product_family": ["smartphone"]
    },
    "boost_terms": ["smartphone", "telephone", "mobile"],
    "intent": "search",
    "explanation": "...",
    "context_used": false,
    "context_summary": ""
  },
  "hits": [
    {
      "source_id": "03660ed0-cf40-4de0-b0d5-d30786b73fe6",
      "sku": "P5378552",
      "name": "Sony PULSE Elite - Micro-casque",
      "brand": "Sony",
      "categories": ["Casques circum-auriculaires et supra-auriculaires"],
      "price": null,
      "currency": "EUR",
      "in_stock": true,
      "matched_terms": ["sony"],
      "matched_filters": {}
    }
  ],
  "total_candidates": 1,
  "retrieval_backend": "typesense"
}
```

### Champs a utiliser cote site

Pour afficher une liste de resultats :

- `hits[].name`
- `hits[].brand`
- `hits[].categories`
- `hits[].price`
- `hits[].currency`
- `hits[].in_stock`

Pour ouvrir la fiche produit :

- `hits[].sku`
- `hits[].source_id`

Le site client doit mapper `sku` ou `source_id` vers sa propre URL produit. Torreto ne force pas le format de route du site hote.

### Detail produit

Endpoint :

```http
GET /api/v1/catalog/{sku}
```

Usage :

- demo
- back-office
- affichage detail a la demande

Pour une integration site classique, ce endpoint n'est pas obligatoire si le site sait deja charger sa fiche produit.

## Memoire conversationnelle

Torreto garde le contexte par couple :

```text
visitor_id + session_id
```

Le site doit :

1. creer ou recuperer un `visitor_id`
2. creer un `session_id` au debut d'une recherche
3. renvoyer `previous_context` a chaque follow-up
4. generer un nouveau `session_id` ou envoyer `reset_context=true` quand l'utilisateur clique sur reinitialiser

Exemple :

```text
Utilisateur: dell
Torreto: brand=Dell

Utilisateur: ordinateur
Torreto: brand=Dell + product_family=computer
```

Autre exemple :

```text
Utilisateur: iphone
Torreto: brand=Apple + product_family=smartphone

Utilisateur: 128 go
Torreto: brand=Apple + product_family=smartphone + storage=128 go
```

## Exemple d'integration frontend

```ts
type SearchState = {
  sessionId: string;
  visitorId: string;
  previousContext: unknown | null;
};

async function search(query: string, state: SearchState) {
  const response = await fetch("https://api.example.com/api/v1/search", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      query,
      limit: 6,
      session_id: state.sessionId,
      visitor_id: state.visitorId,
      reset_context: false,
      previous_context: state.previousContext
    })
  });

  if (!response.ok) {
    throw new Error("Search failed");
  }

  const payload = await response.json();
  state.sessionId = payload.session_id;
  state.previousContext = payload.structured_query;
  return payload.hits;
}

function openProduct(hit: { sku: string }) {
  window.location.href = `/produit/${encodeURIComponent(hit.sku)}`;
}
```

Reset :

```ts
function resetSearch(state: SearchState) {
  state.sessionId = crypto.randomUUID();
  state.previousContext = null;
}
```

## Recherche par SKU ou reference

La recherche exacte par reference est geree en priorite.

Exemples :

```text
P5378552
SKU: P5378552
```

Dans ces cas, Torreto retourne directement le produit correspondant si le `sku` existe.

## Scripts utiles

```bash
scripts/health.sh
scripts/full-sync.sh
scripts/delta-sync.sh
scripts/search.sh "ordinateur dell"
```

Les scripts utilisent `API_BASE`.

Exemple :

```bash
API_BASE=https://api.example.com scripts/delta-sync.sh
```

## Endpoints disponibles

| Methode | Endpoint | Role |
| --- | --- | --- |
| `GET` | `/health` | etat backend, Typesense, catalogue |
| `GET` | `/api/v1/catalog?limit=20` | preview catalogue |
| `GET` | `/api/v1/catalog/{sku}` | detail produit |
| `POST` | `/api/v1/catalog/reindex` | full ou delta sync |
| `POST` | `/api/v1/search` | recherche intelligente |

## Tests

```bash
PYTHONPATH=backend python3 -m unittest discover -s backend/tests
npm --prefix frontend run build
```

## Fichiers runtime non versionnes

Ces fichiers sont generes localement et exclus du Git :

- `backend/data/runtime/`
- `backend/data/runtime-test/`
- `.env`
- `frontend/.next/`
- `node_modules/`

## Integration dans un site existant

Pour l'equipe frontend, le flux recommande est :

1. afficher une barre de recherche dans le header ou la page catalogue
2. creer un `visitor_id` stable cote navigateur
3. creer un `session_id` pour la recherche courante
4. appeler `POST /api/v1/search` a la validation utilisateur
5. afficher les `hits`
6. stocker `structured_query` comme `previous_context`
7. au clic produit, ouvrir la fiche du site avec `sku` ou `source_id`
8. au reset, vider les resultats et generer un nouveau `session_id`

## Autocomplete

L'autocomplete type Amazon n'est pas encore implemente.

La brique recommandee sera :

```http
GET /api/v1/suggest?q=ord&session_id=...&visitor_id=...
```

Elle devra mixer :

- prefix search Typesense
- marques
- categories
- familles produit
- attributs frequents
- contexte de session

Le site gardera l'affichage du dropdown, Torreto fournira les suggestions.

## Limites connues

- memoire conversationnelle en RAM, non partagee entre plusieurs instances
- suppression miroir Akeneo pas encore automatique
- full sync encore synchrone
- pas encore d'endpoint d'autocomplete
- pas encore de `product_url` native dans la reponse

## Documentation complementaire

- [Architecture](docs/architecture.md)
- [Documentation d'exploitation](docs/exploitation.md)

