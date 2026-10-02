// All calls go through the dev-server proxy (/api -> backend); no secrets in browser code.

async function request(path, options = {}) {
  const res = await fetch(`/api${path}`, options);
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON error body */
  }
  if (!res.ok) {
    const messages = {
      401: "The UI is not authorized to call the weather service (check WX_API_KEY).",
      404: "That city could not be found.",
      422: "That city or country was not accepted.",
      429: "Too many requests. Please wait a moment and try again.",
      503: "The weather provider is temporarily unavailable. Try again shortly.",
      504: "The request timed out. Try again.",
    };
    throw new Error(messages[res.status] || body?.detail || `Request failed (${res.status})`);
  }
  return body;
}

export function fetchWeather(city, countryCode, signal) {
  const q = new URLSearchParams({ city, country_code: countryCode });
  return request(`/weather?${q}`, { signal });
}

export function askAgent(question, signal) {
  return request("/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
    signal,
  });
}
