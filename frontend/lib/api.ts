export type SearchHit = {
  source_id: string;
  sku: string;
  name: string;
  brand: string;
  categories: string[];
  price: number | null;
  currency: string;
  in_stock: boolean;
  matched_terms: string[];
  matched_filters: Record<string, string[]>;
};

export type StructuredQuery = {
  raw_query: string;
  normalized_text: string;
  keywords: string[];
  filters: Record<string, string[]>;
  boost_terms: string[];
  intent: string;
  explanation: string;
  context_used: boolean;
  context_summary: string;
};

export type SearchResponse = {
  query: string;
  session_id: string;
  assistant_message: string;
  suggestion_chips: string[];
  structured_query: StructuredQuery;
  hits: SearchHit[];
  total_candidates: number;
  retrieval_backend: string;
};

export type HealthResponse = {
  status: string;
  products: number;
  catalog_mode: string;
  catalog_source: string;
  typesense_status: string;
  typesense_indexed_products: number;
};

export type CatalogProduct = {
  source_id: string;
  sku: string;
  name: string;
  brand: string;
  categories: string[];
  attributes: Record<string, string[]>;
  price: number | null;
  currency: string;
  in_stock: boolean;
  popularity: number;
};

export type ProductDetail = {
  source_id: string;
  sku: string;
  name: string;
  description: string;
  brand: string;
  categories: string[];
  attributes: Record<string, string[]>;
  price: number | null;
  currency: string;
  in_stock: boolean;
  popularity: number;
};

export type ReindexResponse = {
  indexed_products: number;
  source: string;
  source_path: string;
};

function inferApiBase() {
  if (typeof window !== "undefined") {
    const protocol = window.location.protocol === "https:" ? "https:" : "http:";
    const hostname = window.location.hostname || "127.0.0.1";
    return `${protocol}//${hostname}:8000`;
  }

  return "http://127.0.0.1:8000";
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? inferApiBase();

export async function runSearch(
  query: string,
  limit = 6,
  options?: {
    sessionId?: string | null;
    visitorId?: string | null;
    resetContext?: boolean;
    previousContext?: StructuredQuery | null;
  }
): Promise<SearchResponse> {
  const response = await fetch(`${API_BASE}/api/v1/search`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      query,
      limit,
      session_id: options?.sessionId ?? undefined,
      visitor_id: options?.visitorId ?? undefined,
      reset_context: options?.resetContext ?? false,
      previous_context: options?.previousContext ?? undefined
    }),
    cache: "no-store"
  });

  if (!response.ok) {
    throw new Error("La recherche a echoue.");
  }

  return response.json();
}

export async function getHealth(): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE}/health`, {
    cache: "no-store"
  });

  if (!response.ok) {
    throw new Error("Impossible de recuperer l'etat du backend.");
  }

  return response.json();
}

export async function getCatalogPreview(
  limit = 8
): Promise<CatalogProduct[]> {
  const response = await fetch(`${API_BASE}/api/v1/catalog?limit=${limit}`, {
    cache: "no-store"
  });

  if (!response.ok) {
    throw new Error("Impossible de charger le catalogue.");
  }

  return response.json();
}

export async function getProductDetail(sku: string): Promise<ProductDetail> {
  const response = await fetch(`${API_BASE}/api/v1/catalog/${encodeURIComponent(sku)}`, {
    cache: "no-store"
  });

  if (!response.ok) {
    throw new Error("Impossible de charger le detail du produit.");
  }

  return response.json();
}

export async function reindexCatalog(options: {
  source: "sample" | "akeneo";
  maxItems?: number | null;
}): Promise<ReindexResponse> {
  const response = await fetch(`${API_BASE}/api/v1/catalog/reindex`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      source: options.source,
      max_items: options.maxItems ?? undefined
    }),
    cache: "no-store"
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || "La reindexation a echoue.");
  }

  return response.json();
}
