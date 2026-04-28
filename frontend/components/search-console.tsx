"use client";

import { FormEvent, useEffect, useState } from "react";

import {
  CatalogProduct,
  HealthResponse,
  ProductDetail,
  SearchHit,
  SearchResponse,
  StructuredQuery,
  getCatalogPreview,
  getHealth,
  getProductDetail,
  reindexCatalog,
  runSearch
} from "@/lib/api";

const DEFAULT_RESULT_LIMIT = 6;
const RESULT_STEP = 6;
const RESULT_LIMIT_CAP = 60;
const PREFETCH_RESULT_LIMIT = 12;

const FILTER_LABELS: Record<string, string> = {
  brand: "marque",
  categories: "categorie",
  color: "couleur",
  screen_size: "taille d'ecran",
  storage: "stockage"
};

function formatPrice(hit: SearchHit | CatalogProduct) {
  if (hit.price === null) {
    return "Prix non renseigne";
  }

  return `${hit.price.toFixed(0)} ${hit.currency}`;
}

function formatRetrievalBackend(backend: string) {
  if (backend.startsWith("typesense-relaxed")) {
    return "Typesense approx.";
  }

  return backend === "typesense" ? "Typesense" : "Moteur local";
}

function formatTypesenseStatus(
  status: string | undefined,
  options?: { loading?: boolean; error?: boolean }
) {
  if (options?.loading) {
    return "Verification en cours";
  }

  if (options?.error) {
    return "Backend indisponible";
  }

  if (status === "ok") {
    return "Typesense actif";
  }

  if (status === "disabled") {
    return "Typesense desactive";
  }

  if (status === "unreachable") {
    return "Typesense indisponible";
  }

  return "Verification en cours";
}

function createSessionId() {
  return `session-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function createVisitorId() {
  return `visitor-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function formatFilterLabel(key: string) {
  return FILTER_LABELS[key] ?? key.replaceAll("_", " ");
}

function buildSearchSummary(result: SearchResponse) {
  if (result.retrieval_backend.startsWith("typesense-relaxed")) {
    return result.assistant_message;
  }

  const filterSummary = Object.entries(result.structured_query.filters)
    .map(([key, values]) => `${formatFilterLabel(key)}: ${values.join(", ")}`)
    .join(" · ");
  const keywordSummary = result.structured_query.keywords.slice(0, 4).join(", ");

  if (result.hits.length === 0) {
    return `Aucun produit du dataset actif ne correspond clairement a "${result.query}".`;
  }

  if (filterSummary) {
    return `Resultats relies a la requete via ${filterSummary}${keywordSummary ? `, avec les termes ${keywordSummary}.` : "."}`;
  }

  if (keywordSummary) {
    return `Resultats relies a la requete via les termes detectes: ${keywordSummary}.`;
  }

  return "Resultats remontes a partir de la requete comprise par le moteur.";
}

function buildMatchReasons(hit: SearchHit) {
  const reasons = Object.entries(hit.matched_filters).map(
    ([key, values]) => `${formatFilterLabel(key)}: ${values.join(", ")}`
  );

  if (hit.matched_terms.length > 0) {
    reasons.push(`termes reconnus: ${hit.matched_terms.slice(0, 5).join(", ")}`);
  } else if (reasons.length === 0) {
    reasons.push("correspondance retenue par le moteur");
  }

  return reasons;
}

function buildDetailAttributes(detail: ProductDetail) {
  const priorityKeys = ["product_family", "screen_size", "storage", "color"];
  const selected: Array<[string, string[]]> = [];

  for (const key of priorityKeys) {
    const values = detail.attributes[key];
    if (values?.length) {
      selected.push([key, values]);
    }
  }

  for (const [key, values] of Object.entries(detail.attributes)) {
    if (!values.length || priorityKeys.includes(key)) {
      continue;
    }
    selected.push([key, values]);
    if (selected.length >= 8) {
      break;
    }
  }

  return selected.slice(0, 8);
}

export function SearchConsole() {
  const [draftQuery, setDraftQuery] = useState("");
  const [sessionId, setSessionId] = useState(createSessionId);
  const [visitorId, setVisitorId] = useState(createVisitorId);
  const [visibleLimit, setVisibleLimit] = useState(DEFAULT_RESULT_LIMIT);
  const [catalogSource, setCatalogSource] = useState<"sample" | "akeneo">(
    "sample"
  );
  const [maxItems, setMaxItems] = useState("100");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [catalogPreview, setCatalogPreview] = useState<CatalogProduct[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [workspaceLoading, setWorkspaceLoading] = useState(true);
  const [reindexLoading, setReindexLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [reindexFeedback, setReindexFeedback] = useState<string | null>(null);
  const [expandedSku, setExpandedSku] = useState<string | null>(null);
  const [productDetails, setProductDetails] = useState<Record<string, ProductDetail>>({});
  const [detailLoadingSku, setDetailLoadingSku] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  async function loadWorkspace({ silent = false } = {}) {
    if (!silent) {
      setWorkspaceLoading(true);
    }
    setWorkspaceError(null);

    try {
      const [healthPayload, catalogPayload] = await Promise.all([
        getHealth(),
        getCatalogPreview(6)
      ]);
      setHealth(healthPayload);
      setCatalogPreview(catalogPayload);

      if (
        healthPayload.catalog_mode === "sample" ||
        healthPayload.catalog_mode === "akeneo"
      ) {
        setCatalogSource(healthPayload.catalog_mode);
      }
    } catch (error) {
      setWorkspaceError(
        error instanceof Error
          ? error.message
          : "Impossible de charger l'etat de la demo."
      );
    } finally {
      if (!silent) {
        setWorkspaceLoading(false);
      }
    }
  }

  async function performSearch(
    nextQuery: string,
    options?: {
      sessionId?: string;
      visitorId?: string;
      resetContext?: boolean;
      displayLimit?: number;
      fetchLimit?: number;
      previousContext?: StructuredQuery | null;
    }
  ) {
    setSearchLoading(true);
    setSearchError(null);

    try {
      const nextDisplayLimit = Math.max(
        1,
        Math.min(options?.displayLimit ?? visibleLimit, RESULT_LIMIT_CAP)
      );
      const requestedLimit = Math.max(
        nextDisplayLimit,
        Math.max(
          1,
          Math.min(options?.fetchLimit ?? nextDisplayLimit, RESULT_LIMIT_CAP)
        )
      );
      const payload = await runSearch(nextQuery, requestedLimit, {
        sessionId: options?.sessionId ?? sessionId,
        visitorId: options?.visitorId ?? visitorId,
        resetContext: options?.resetContext ?? false,
        previousContext:
          options?.resetContext ?? false
            ? null
            : options?.previousContext ?? result?.structured_query ?? null
      });
      setResult(payload);
      setDraftQuery("");
      setVisibleLimit(nextDisplayLimit);
      setExpandedSku(null);
      setDetailError(null);
      setSessionId(payload.session_id);
      if (typeof window !== "undefined") {
        window.sessionStorage.setItem("smart-search-session-id", payload.session_id);
      }
    } catch (error) {
      setSearchError(
        error instanceof Error
          ? error.message
          : "Une erreur est survenue pendant la recherche."
      );
    } finally {
      setSearchLoading(false);
    }
  }

  function startFreshSearch(
    nextQuery: string,
    options?: {
      sessionId?: string;
      visitorId?: string;
      resetContext?: boolean;
    }
  ) {
    return performSearch(nextQuery, {
      ...options,
      displayLimit: DEFAULT_RESULT_LIMIT,
      fetchLimit: PREFETCH_RESULT_LIMIT
    });
  }

  async function handleReindex() {
    setReindexLoading(true);
    setReindexFeedback(null);
    setWorkspaceError(null);

    try {
      const parsedMaxItems = Number(maxItems);
      const payload = await reindexCatalog({
        source: catalogSource,
        maxItems:
          catalogSource === "akeneo" && Number.isFinite(parsedMaxItems) && parsedMaxItems > 0
            ? parsedMaxItems
            : undefined
      });
      setReindexFeedback(
        `${payload.indexed_products} produits reindexes depuis ${payload.source}.`
      );
      await loadWorkspace({ silent: true });
      const nextSessionId = createSessionId();
      setSessionId(nextSessionId);
      setVisibleLimit(DEFAULT_RESULT_LIMIT);
      if (typeof window !== "undefined") {
        window.sessionStorage.setItem("smart-search-session-id", nextSessionId);
      }
      setExpandedSku(null);
      setDetailError(null);
      if (result?.query) {
        await startFreshSearch(result.query, {
          sessionId: nextSessionId,
          resetContext: true
        });
      } else {
        setResult(null);
      }
    } catch (error) {
      setWorkspaceError(
        error instanceof Error ? error.message : "La reindexation a echoue."
      );
    } finally {
      setReindexLoading(false);
    }
  }

  useEffect(() => {
    void (async () => {
      if (typeof window !== "undefined") {
        const storedVisitorId =
          window.localStorage.getItem("smart-search-visitor-id") ?? createVisitorId();
        const storedSessionId =
          window.sessionStorage.getItem("smart-search-session-id") ?? createSessionId();
        window.localStorage.setItem("smart-search-visitor-id", storedVisitorId);
        window.sessionStorage.setItem("smart-search-session-id", storedSessionId);
        setVisitorId(storedVisitorId);
        setSessionId(storedSessionId);
        await loadWorkspace();
        return;
      }

      await loadWorkspace();
    })();
  }, []);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedQuery = draftQuery.trim();
    if (!trimmedQuery) {
      return;
    }
    void startFreshSearch(trimmedQuery);
  }

  function handleResetSearch() {
    const nextSessionId = createSessionId();
    setSessionId(nextSessionId);
    setVisibleLimit(DEFAULT_RESULT_LIMIT);
    if (typeof window !== "undefined") {
      window.sessionStorage.setItem("smart-search-session-id", nextSessionId);
    }
    setDraftQuery("");
    setResult(null);
    setSearchError(null);
    setExpandedSku(null);
    setDetailError(null);
  }

  function handleShowMore() {
    if (!result) {
      return;
    }

    const maxDisplayable = Math.min(result.total_candidates, RESULT_LIMIT_CAP);
    const nextVisibleLimit = Math.min(visibleLimit + RESULT_STEP, maxDisplayable);

    if (nextVisibleLimit <= result.hits.length) {
      setVisibleLimit(nextVisibleLimit);
      return;
    }

    void performSearch(result.query, {
      sessionId: result.session_id,
      visitorId,
      displayLimit: nextVisibleLimit,
      fetchLimit: Math.min(nextVisibleLimit + RESULT_STEP, maxDisplayable),
      previousContext: result.structured_query
    });
  }

  function handleShowAll() {
    if (!result) {
      return;
    }

    const maxDisplayable = Math.min(result.total_candidates, RESULT_LIMIT_CAP);
    if (maxDisplayable <= result.hits.length) {
      setVisibleLimit(maxDisplayable);
      return;
    }

    void performSearch(result.query, {
      sessionId: result.session_id,
      visitorId,
      displayLimit: maxDisplayable,
      fetchLimit: maxDisplayable,
      previousContext: result.structured_query
    });
  }

  const visibleHits = result ? result.hits.slice(0, visibleLimit) : [];

  async function handleToggleDetail(hit: SearchHit) {
    if (expandedSku === hit.sku) {
      setExpandedSku(null);
      return;
    }

    setExpandedSku(hit.sku);
    setDetailError(null);

    if (productDetails[hit.sku]) {
      return;
    }

    setDetailLoadingSku(hit.sku);
    try {
      const detail = await getProductDetail(hit.sku);
      setProductDetails((current) => ({ ...current, [hit.sku]: detail }));
    } catch (error) {
      setDetailError(
        error instanceof Error ? error.message : "Impossible de charger le detail du produit."
      );
    } finally {
      setDetailLoadingSku((current) => (current === hit.sku ? null : current));
    }
  }

  return (
    <div className="page-grid demo-page">
      <section className="hero-card demo-hero">
        <div className="demo-topline">
          <span className="eyebrow">Smart search demo</span>
          <span className="demo-soft-pill">
            {formatTypesenseStatus(health?.typesense_status, {
              loading: workspaceLoading && !health,
              error: Boolean(workspaceError) && !health
            })}
          </span>
        </div>

        <h1 className="demo-title">
          Une barre de recherche qui comprend ce que l'utilisateur veut dire.
        </h1>
        <p className="demo-copy">
          Pensee pour s'integrer dans un site e-commerce sans exposer la
          complexite technique. Ici, on demontre surtout une chose: la recherche
          intelligente retrouve le bon produit meme quand la requete est
          imprecise, courte ou tres humaine.
        </p>

        <form className="search-stage" onSubmit={handleSubmit}>
          <div className="search-stage-shell">
            <input
              className="search-stage-input"
              value={draftQuery}
              onChange={(event) => setDraftQuery(event.target.value)}
              placeholder={
                result?.assistant_message ??
                "Exemple: sony, pc 20 pouces, ordinateur portable"
              }
            />
            <button
              className="search-stage-button"
              type="submit"
              disabled={searchLoading}
            >
              {searchLoading ? "Recherche..." : "Rechercher"}
            </button>
            <button
              className="secondary-button search-reset-button"
              type="button"
              onClick={handleResetSearch}
            >
              Reinitialiser
            </button>
          </div>
        </form>

        {result?.structured_query.context_summary ? (
          <div className="search-context-row">
            <span className="demo-soft-pill memory-pill">
              Contexte actif: {result.structured_query.context_summary}
            </span>
          </div>
        ) : null}

        {result && result.suggestion_chips.length > 0 ? (
          <div className="quick-query-row demo-quick-row">
            {result.suggestion_chips.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                className="quick-query"
                onClick={() => void startFreshSearch(suggestion)}
              >
                {suggestion}
              </button>
            ))}
          </div>
        ) : null}

      </section>

      {searchError ? <p className="error-state">{searchError}</p> : null}

      <section className="panel demo-results-panel">
        <div className="demo-results-head">
          <div>
            <h2 className="panel-title">Resultats</h2>
            <p className="panel-copy">
              {result
                ? buildSearchSummary(result)
                : "Une presentation volontairement simple, proche d'une integration sur un site client."}
            </p>
          </div>
          {result ? (
            <div className="demo-summary-pills">
              <span className="demo-soft-pill">
                {formatRetrievalBackend(result.retrieval_backend)}
              </span>
              <span className="demo-soft-pill">
                {result.total_candidates} candidats
              </span>
            </div>
          ) : null}
        </div>

        {result && visibleHits.length > 0 ? (
          <>
            <div className="results-list">
              {visibleHits.map((hit, index) => (
                <article key={hit.sku} className="result-card demo-result-card">
                  {(() => {
                    const reasons = buildMatchReasons(hit);
                    const detail = productDetails[hit.sku];
                    const detailAttributes = detail ? buildDetailAttributes(detail) : [];
                    const isExpanded = expandedSku === hit.sku;
                    const isLoadingDetail = detailLoadingSku === hit.sku;

                    return (
                      <>
                  <div className="result-header">
                    <div>
                      <span className="eyebrow">Top {index + 1}</span>
                      <h3 className="result-title">{hit.name}</h3>
                      <p className="result-meta">
                        {hit.brand} - {hit.categories.join(" / ")}
                      </p>
                    </div>

                    <div className="price-stack">
                      <span className="price-badge">{formatPrice(hit)}</span>
                      <span className={`stock-badge ${hit.in_stock ? "" : "out"}`}>
                        {hit.in_stock ? "En stock" : "Rupture"}
                      </span>
                    </div>
                  </div>
                  <div className="result-inline-actions">
                    <button
                      type="button"
                      className="secondary-button compact-detail-button"
                      onClick={() => void handleToggleDetail(hit)}
                    >
                      {isExpanded ? "Masquer le detail" : "Voir le detail"}
                    </button>
                    <span className="subtle-copy selection-copy">SKU: {hit.sku}</span>
                  </div>
                  {isExpanded ? (
                    <div className="result-detail-panel">
                      {isLoadingDetail ? (
                        <p className="subtle-copy">Chargement du detail produit...</p>
                      ) : detail ? (
                        <>
                          {detail.description ? (
                            <p className="result-description">{detail.description}</p>
                          ) : (
                            <p className="subtle-copy">
                              Aucun descriptif long n'est disponible pour ce produit.
                            </p>
                          )}
                          {reasons.length > 0 ? (
                            <div className="tag-list">
                              {reasons.map((reason) => (
                                <span key={`${hit.sku}-${reason}`} className="tag">
                                  {reason}
                                </span>
                              ))}
                            </div>
                          ) : null}
                          {detailAttributes.length > 0 ? (
                            <div className="tag-list">
                              {detailAttributes.map(([key, values]) => (
                                <span key={`${hit.sku}-${key}`} className="tag">
                                  {formatFilterLabel(key)}: {values.slice(0, 3).join(", ")}
                                </span>
                              ))}
                            </div>
                          ) : null}
                          <p className="subtle-copy selection-copy">
                            Selection technique: source_id {detail.source_id}
                          </p>
                        </>
                      ) : detailError ? (
                        <p className="error-state">{detailError}</p>
                      ) : null}
                    </div>
                  ) : null}
                      </>
                    );
                  })()}
                </article>
              ))}
            </div>

            {result.total_candidates > visibleHits.length ? (
              <div className="results-actions">
                <p className="subtle-copy results-counter">
                  {visibleHits.length} resultats affiches sur {result.total_candidates}.
                </p>
                {visibleHits.length < Math.min(result.total_candidates, RESULT_LIMIT_CAP) ? (
                  <div className="button-row results-button-row">
                    <button
                      type="button"
                      className="secondary-button"
                      disabled={searchLoading}
                      onClick={handleShowMore}
                    >
                      {searchLoading ? "Chargement..." : "Afficher plus"}
                    </button>
                    <button
                      type="button"
                      className="secondary-button"
                      disabled={searchLoading}
                      onClick={handleShowAll}
                    >
                      {result.total_candidates > RESULT_LIMIT_CAP
                        ? `Afficher les ${RESULT_LIMIT_CAP} premiers`
                        : "Tout afficher"}
                    </button>
                  </div>
                ) : null}
                {result.total_candidates > RESULT_LIMIT_CAP ? (
                  <p className="subtle-copy results-note">
                    Pour garder une demo fluide, l'affichage extensible est limite aux{" "}
                    {RESULT_LIMIT_CAP} premiers resultats.
                  </p>
                ) : null}
              </div>
            ) : null}
          </>
        ) : result ? (
          <div className="metric-card">
            <p className="metric-label">Aucun resultat pertinent</p>
            <p className="metric-value">
              Aucun produit ne correspond clairement a la requete
              <strong> {result.query}</strong> dans le dataset actif. Essaie une
              autre marque, une categorie, ou recharge un autre catalogue.
            </p>
          </div>
        ) : (
          <p className="empty-state">
            Lance une recherche pour afficher les produits correspondants.
          </p>
        )}
      </section>

      <details className="panel demo-details">
        <summary className="demo-details-summary">
          Voir les details techniques de la recherche
        </summary>

        <div className="demo-details-body">
          {result ? (
            <div className="demo-technical-grid">
              <div className="metric-card">
                <p className="metric-label">Requete normalisee</p>
                <p className="metric-value">
                  {result.structured_query.normalized_text}
                </p>
              </div>

              <div className="metric-card">
                <p className="metric-label">Mots cles</p>
                <div className="tag-list">
                  {result.structured_query.keywords.map((keyword) => (
                    <span key={keyword} className="tag">
                      {keyword}
                    </span>
                  ))}
                </div>
              </div>

              <div className="metric-card">
                <p className="metric-label">Filtres detectes</p>
                {Object.keys(result.structured_query.filters).length > 0 ? (
                  <div className="tag-list">
                    {Object.entries(result.structured_query.filters).map(
                      ([key, values]) => (
                        <span key={key} className="tag">
                          {key}: {values.join(", ")}
                        </span>
                      )
                    )}
                  </div>
                ) : (
                  <p className="metric-value">Aucun filtre strict.</p>
                )}
              </div>

              <div className="metric-card">
                <p className="metric-label">Expansion semantique</p>
                {result.structured_query.boost_terms.length > 0 ? (
                  <div className="tag-list">
                    {result.structured_query.boost_terms.map((term) => (
                      <span key={term} className="tag">
                        {term}
                      </span>
                    ))}
                  </div>
                ) : (
                  <p className="metric-value">Pas d'expansion declenchee.</p>
                )}
              </div>

              <div className="metric-card">
                <p className="metric-label">Explication</p>
                <p className="metric-value">{result.structured_query.explanation}</p>
              </div>

            </div>
          ) : null}
        </div>
      </details>

      <details className="panel demo-details">
        <summary className="demo-details-summary">
          Reindexation et dataset de test
        </summary>

        <div className="demo-details-body">
          <div className="demo-ops-grid">
            <div className="metric-card">
              <p className="metric-label">Etat systeme</p>
              {workspaceLoading ? (
                <p className="metric-value">Chargement de l'etat...</p>
              ) : (
                <div className="status-list">
                  <div className="status-kv">
                    <span>Produits charges</span>
                    <strong>{health?.products ?? 0}</strong>
                  </div>
                  <div className="status-kv">
                    <span>Produits indexes Typesense</span>
                    <strong>{health?.typesense_indexed_products ?? 0}</strong>
                  </div>
                  <div className="status-kv">
                    <span>Source active</span>
                    <strong>{health?.catalog_source ?? "n/a"}</strong>
                  </div>
                </div>
              )}
            </div>

            <div className="metric-card">
              <p className="metric-label">Reindexation</p>
              <div className="mode-switch">
                <button
                  type="button"
                  className={`mode-button ${catalogSource === "sample" ? "active" : ""}`}
                  onClick={() => setCatalogSource("sample")}
                >
                  Sample
                </button>
                <button
                  type="button"
                  className={`mode-button ${catalogSource === "akeneo" ? "active" : ""}`}
                  onClick={() => setCatalogSource("akeneo")}
                >
                  Akeneo
                </button>
              </div>

              <div className="field-row">
                <label className="field-block">
                  <span className="metric-label">Max items Akeneo</span>
                  <input
                    className="small-input"
                    value={maxItems}
                    onChange={(event) => setMaxItems(event.target.value)}
                    inputMode="numeric"
                    placeholder="100"
                  />
                </label>
              </div>

              <div className="button-row">
                <button
                  type="button"
                  className="search-button"
                  disabled={reindexLoading}
                  onClick={() => void handleReindex()}
                >
                  {reindexLoading ? "Reindexation..." : "Lancer la reindexation"}
                </button>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => void loadWorkspace()}
                >
                  Rafraichir
                </button>
              </div>

              <p className="subtle-copy">
                Mode actuel: full refresh, pas encore de sync differentielle.
              </p>
              {reindexFeedback ? <p className="success-state">{reindexFeedback}</p> : null}
              {workspaceError ? <p className="error-state">{workspaceError}</p> : null}
            </div>
          </div>

          <div className="demo-catalog-preview">
            <p className="metric-label">Apercu du dataset charge</p>
            <div className="catalog-list">
              {catalogPreview.map((product) => (
                <article className="catalog-item" key={product.sku}>
                  <div className="catalog-item-head">
                    <div>
                      <h3 className="catalog-title">{product.name}</h3>
                      <p className="result-meta">
                        {product.brand} - {product.categories.join(" / ")}
                      </p>
                    </div>
                    <span className={`stock-badge ${product.in_stock ? "" : "out"}`}>
                      {product.in_stock ? "En stock" : "Rupture"}
                    </span>
                  </div>
                  <div className="tag-list">
                    <span className="tag">{product.sku}</span>
                    <span className="tag">{formatPrice(product)}</span>
                  </div>
                </article>
              ))}
            </div>
          </div>
        </div>
      </details>
    </div>
  );
}
