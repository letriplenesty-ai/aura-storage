import { test, expect } from "@playwright/test";
import type { Page, Route } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

/**
 * Upload journal: an import interrupted by a reload can be resumed, and only
 * the files the server never confirmed are sent again. API and presigned PUT
 * are mocked. Files are written to disk so their modification time is stable
 * between the two selections (the journal fingerprint includes it).
 */

const ME = {
  id: "u1", kind: "user", name: "admin", role: "admin", space_id: "s1",
  application_id: "a1", scopes: ["objects:read", "objects:write", "objects:delete"],
  csrf_token: "csrf-test",
};

async function installMocks(page: Page, state: { failBad: boolean; intents: string[] }) {
  await page.route("**/api/v1/**", async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (path === "/api/v1/login") return route.fulfill({ json: { csrf_token: "csrf-test" } });
    if (path === "/api/v1/me") return route.fulfill({ json: ME });
    if (path === "/api/v1/folders") return route.fulfill({ json: { items: [] } });
    if (path === "/api/v1/objects" && method === "GET") {
      return route.fulfill({ json: { items: [] } });
    }
    if (path === "/api/v1/objects/upload-intents" && method === "POST") {
      const body = request.postDataJSON() as { display_name: string; size_bytes: number };
      state.intents.push(body.display_name);
      const id = `obj-${body.display_name}`;
      return route.fulfill({
        json: {
          id, status: "pending", method: "PUT", upload_url: `/fake-s3/put/${id}`,
          headers: { "Content-Type": "text/plain" }, content_type: "text/plain",
          size_bytes: body.size_bytes, expires_in: 900,
          expires_at: Math.floor(Date.now() / 1000) + 900, idempotent_replay: false,
        },
      });
    }
    const commit = path.match(/\/api\/v1\/objects\/([^/]+)\/commit$/);
    if (commit && method === "POST") {
      const id = decodeURIComponent(commit[1] ?? "");
      if (id.includes("bad") && state.failBad) {
        return route.fulfill({ status: 422, json: { detail: "uploaded size does not match storage" } });
      }
      return route.fulfill({ json: { id, status: "available", size_bytes: 4, checksum_sha256: "abc" } });
    }
    return route.fulfill({ status: 404, json: { detail: `unmocked ${method} ${path}` } });
  });
  await page.route("**/fake-s3/**", (route) => route.fulfill({ status: 200, body: "" }));
}

test("an interrupted import resumes with only the missing files", async ({ page }) => {
  const dir = test.info().outputPath("files");
  mkdirSync(dir, { recursive: true });
  const files = ["alpha.txt", "bad.txt"].map((name) => {
    const path = join(dir, name);
    writeFileSync(path, "data");
    return path;
  });

  const state = { failBad: true, intents: [] as string[] };
  await installMocks(page, state);
  await page.goto("/login");
  await page.getByLabel("Identifiant").fill("admin");
  await page.getByLabel("Mot de passe").fill("x");
  await page.getByRole("button", { name: "Se connecter", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Mes fichiers" })).toBeVisible();

  await page.getByTestId("files-input").setInputFiles(files);
  await page.getByRole("button", { name: "Importer et valider" }).click();
  const status = page.getByTestId("transfer-status");
  await expect(status.getByText("Disponible", { exact: true })).toHaveCount(1);
  await expect(status.getByText("Échec", { exact: true })).toHaveCount(1);

  // The tab is reloaded: the in-page queue is gone, the journal is not.
  state.failBad = false;
  state.intents = [];
  await page.reload();
  const banner = page.getByRole("region", { name: "Import interrompu" });
  await expect(banner).toContainText("1 fichier(s) restant(s)");

  // Selecting both files again sends only the one never confirmed.
  await page.getByTestId("resume-files-input").setInputFiles(files);
  await expect(status.getByText("Disponible", { exact: true })).toHaveCount(1);
  expect(state.intents).toEqual(["bad.txt"]);
  await expect(banner).toHaveCount(0);
});

test("forgetting an interrupted import clears the banner", async ({ page }) => {
  const dir = test.info().outputPath("files");
  mkdirSync(dir, { recursive: true });
  const path = join(dir, "bad.txt");
  writeFileSync(path, "data");
  const state = { failBad: true, intents: [] as string[] };
  await installMocks(page, state);
  await page.goto("/login");
  await page.getByLabel("Identifiant").fill("admin");
  await page.getByLabel("Mot de passe").fill("x");
  await page.getByRole("button", { name: "Se connecter", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Mes fichiers" })).toBeVisible();

  await page.getByTestId("files-input").setInputFiles([path]);
  await page.getByRole("button", { name: "Importer et valider" }).click();
  await expect(page.getByTestId("transfer-status").getByText("Échec", { exact: true })).toHaveCount(1);

  await page.reload();
  const banner = page.getByRole("region", { name: "Import interrompu" });
  await expect(banner).toBeVisible();
  await banner.getByRole("button", { name: "Oublier" }).click();
  await expect(banner).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Mes fichiers" })).toBeVisible();
  await expect(banner).toHaveCount(0);
});
