import { vi } from "vitest";

export interface MockRequest {
  body: BodyInit | null | undefined;
  credentials: RequestCredentials | undefined;
  headers: Headers;
  method: string;
  url: URL;
}

export type MockRoute =
  Response | ((request: MockRequest) => Response | Promise<Response>);

export function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");

  return new Response(JSON.stringify(body), { ...init, headers });
}

export function installFetchMock(routes: Record<string, MockRoute>) {
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const inputRequest = input instanceof Request ? input : undefined;
      const url = new URL(
        inputRequest?.url ?? input.toString(),
        window.location.origin,
      );
      const method = (
        init?.method ??
        inputRequest?.method ??
        "GET"
      ).toUpperCase();
      const headers = new Headers(inputRequest?.headers);

      if (init?.headers) {
        new Headers(init.headers).forEach((value, key) => {
          headers.set(key, value);
        });
      }

      const request: MockRequest = {
        body: init?.body,
        credentials: init?.credentials,
        headers,
        method,
        url,
      };
      const exactKey = `${method} ${url.pathname}${url.search}`;
      const pathKey = `${method} ${url.pathname}`;
      const route = routes[exactKey] ?? routes[pathKey];

      if (!route) {
        throw new Error(`Unexpected fetch: ${exactKey}`);
      }

      return typeof route === "function" ? await route(request) : route.clone();
    },
  );

  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

export function visit(pathname: string): void {
  window.history.pushState({}, "", pathname);
}
