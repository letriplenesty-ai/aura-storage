import { useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "@tanstack/react-router";
import { ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { VaultMark } from "../components/VaultMark";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await login(username, password);
      await navigate({ to: "/" });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Identifiants invalides. Accès refusé.");
      } else if (err instanceof ApiError && err.status === 429) {
        setError("Trop de tentatives. Patientez une minute puis réessayez.");
      } else {
        setError("Connexion impossible : service indisponible ou réseau.");
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="login-wrap">
      <section
        className="login-card vault-login-card"
        aria-labelledby="login-title"
      >
        <div className="login-mark">
          <VaultMark size={34} />
        </div>
        <h1 id="login-title">AURA Vault</h1>
        <p className="muted">Stockage objet chiffré. Connectez-vous pour gérer vos fichiers.</p>
        <form onSubmit={(e) => void handleSubmit(e)} className="form">
          <label className="field">
            <span>Identifiant</span>
            <input
              type="text"
              name="username"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </label>
          <label className="field">
            <span>Mot de passe</span>
            <input
              type="password"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          <button type="submit" className="btn btn-primary" disabled={pending}>
            {pending ? "Connexion…" : "Se connecter"}
          </button>
        </form>
        {error ? (
          <p role="alert" className="alert alert-error">
            {error}
          </p>
        ) : null}
      </section>
    </div>
  );
}
