import { useEffect, useRef, useState } from "react";
import { COUNTRIES } from "./data.js";
import { askAgent, fetchWeather } from "./api.js";

const ICONS = [
  [/thunder/i, "⛈️"], [/snow/i, "❄️"], [/rain|drizzle|shower/i, "🌧️"],
  [/fog/i, "🌫️"], [/overcast/i, "☁️"], [/partly/i, "⛅"], [/mainly clear/i, "🌤️"], [/clear/i, "☀️"],
];
const iconFor = (condition) => ICONS.find(([re]) => re.test(condition))?.[1] ?? "🌡️";

// Render **bold** from the model as <strong>, everything else as plain text (no HTML injection).
const renderInline = (text) =>
  text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4
      ? <strong key={i}>{part.slice(2, -2)}</strong>
      : part
  );

export default function App() {
  const [countryCode, setCountryCode] = useState("IN");
  const country = COUNTRIES.find((c) => c.code === countryCode);
  const [city, setCity] = useState(country.cities[0]);

  const [weather, setWeather] = useState(null);
  const [advice, setAdvice] = useState(null);
  const [status, setStatus] = useState("idle"); // idle | loading | error
  const [adviceStatus, setAdviceStatus] = useState("idle");
  const [error, setError] = useState("");
  const abortRef = useRef(null);

  // Changing country resets to its first popular city.
  const onCountry = (code) => {
    setCountryCode(code);
    setCity(COUNTRIES.find((c) => c.code === code).cities[0]);
  };

  // Fetch whenever the selection changes; abort stale requests so a slow earlier
  // response can never overwrite a newer one.
  useEffect(() => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setStatus("loading");
    setAdvice(null);
    setAdviceStatus("idle");
    fetchWeather(city, countryCode, ctrl.signal)
      .then((data) => {
        setWeather(data);
        setStatus("idle");
        setError("");
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        setWeather(null);
        setError(err.message);
        setStatus("error");
      });
    return () => ctrl.abort();
  }, [city, countryCode]);

  const getAdvice = async () => {
    setAdviceStatus("loading");
    try {
      const res = await askAgent(`Should I carry an umbrella in ${city}, ${country.name}? Keep it short.`);
      setAdvice(res.answer);
      setAdviceStatus("idle");
    } catch (err) {
      setAdvice(err.message);
      setAdviceStatus("error");
    }
  };

  return (
    <main className="app">
      <h1>Weather Agent</h1>
      <p className="sub">Pick a country, then one of its popular cities.</p>

      <div className="controls">
        <label>
          Country
          <select value={countryCode} onChange={(e) => onCountry(e.target.value)}>
            {COUNTRIES.map((c) => (
              <option key={c.code} value={c.code}>{c.name}</option>
            ))}
          </select>
        </label>
        <label>
          City
          <select value={city} onChange={(e) => setCity(e.target.value)}>
            {country.cities.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </label>
      </div>

      <div className="chips" role="group" aria-label="Popular cities">
        {country.cities.map((c) => (
          <button key={c} className={c === city ? "chip active" : "chip"} onClick={() => setCity(c)}>
            {c}
          </button>
        ))}
      </div>

      <section className="card" aria-live="polite">
        {status === "loading" && <p className="muted">Loading weather for {city}…</p>}
        {status === "error" && <p className="error" role="alert">{error}</p>}
        {status === "idle" && weather && (
          <>
            <div className="headline">
              <span className="icon" aria-hidden="true">{iconFor(weather.condition)}</span>
              <div>
                <h2>{weather.city}, {weather.country}</h2>
                <p className="muted">{weather.condition}</p>
              </div>
              <div className="temp">{Math.round(weather.temperature_c)}°C</div>
            </div>
            <dl className="stats">
              <div><dt>Wind</dt><dd>{weather.wind_kmh} km/h</dd></div>
              <div><dt>Precipitation</dt><dd>{weather.precipitation_mm} mm</dd></div>
              <div><dt>Rain chance today</dt><dd>{weather.rain_chance_pct ?? "–"}%</dd></div>
            </dl>
            <button className="primary" onClick={getAdvice} disabled={adviceStatus === "loading"}>
              {adviceStatus === "loading" ? "Asking the agent…" : "Ask the AI: do I need an umbrella?"}
            </button>
            {advice && <p className={adviceStatus === "error" ? "error" : "advice"}>{renderInline(advice)}</p>}
          </>
        )}
      </section>
      <p className="foot">Live data from Open-Meteo. AI advice comes from the agent service and may take a few seconds.</p>
    </main>
  );
}
