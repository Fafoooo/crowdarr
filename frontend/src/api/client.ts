interface ErrorPayload {
  detail?: unknown;
  message?: unknown;
}

export class ApiError extends Error {
  readonly detail: string;
  readonly status: number;

  constructor(status: number, detail: string, options?: ErrorOptions) {
    super(detail, options);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function hasJsonContentType(response: Response): boolean {
  return (
    response.headers.get("content-type")?.includes("application/json") ?? false
  );
}

function readableDetail(payload: ErrorPayload | undefined): string | undefined {
  if (typeof payload?.detail === "string") {
    return payload.detail;
  }
  if (typeof payload?.message === "string") {
    return payload.message;
  }
  return undefined;
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");

  if (
    init.body != null &&
    !(init.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      headers,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new ApiError(0, "Network request failed", { cause: error });
  }

  if (!response.ok) {
    let payload: ErrorPayload | undefined;
    if (hasJsonContentType(response)) {
      try {
        payload = (await response.json()) as ErrorPayload;
      } catch {
        payload = undefined;
      }
    }

    const detail =
      readableDetail(payload) ??
      `Request failed with status ${response.status}`;
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const contentLength = response.headers.get("content-length");
  if (contentLength === "0") {
    return undefined as T;
  }

  if (hasJsonContentType(response)) {
    return (await response.json()) as T;
  }

  const text = await response.text();
  return (text.length > 0 ? text : undefined) as T;
}
