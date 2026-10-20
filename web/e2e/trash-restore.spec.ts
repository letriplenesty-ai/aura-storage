import { test, expect } from "@playwright/test";
import type { Page, Route } from "@playwright/test";

/** Trash and restore, with a mocked API (nothing is created in the catalogue). */

function obj(id: string, name: string, status: string, size: number) {
  return {
    id, display_name: name, content_type: "text/plain", size_bytes: size, status,
    folder_id: null, checksum_sha256: "a".repeat(64),
    created_at: 1_700_000_000, updated_at: 1_700_000_000,
  };
}

async function setup(page: Page, restored: string[]) {
  const trash = [obj("t1", "rapport.txt", "deleted", 12), obj("t2", "jamais-envoye.txt", "deleted", -1)];
  await page.route("**/api/v1/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/login") {
      await route.fulfill({ json: { csrf_token: "csrf-test" } });
    } else if (path === "/api/v1/me") {
      await route.fulfill({
        json: {
          id: "u1", kind: "user", name: "admin", role: "admin", space_id: "s1",
          application_id: "a1", scopes: ["objects:read", "objects:delete"],
          csrf_token: "csrf-test",
        },
      });
    } else if (path === "/api/v1/folders") {
      await route.fulfill({ json: { items: [] } });
    } else if (path === "/api/v1/objects") {
      const status = url.searchParams.get("status");
      await route.fulfill({ json: { items: status === "deleted" ? trash : [] } });
    } else if (path.endsWith("/restore")) {
      restored.push(path.split("/").slice(-2)[0] ?? "");
      await route.fulfill({ json: obj("t1", "rapport.txt", "available", 12) });
    } else {
      await route.fulfill({ status: 404, json: { detail: "not mocked" } });
    }
  });
  await page.goto("/login");
  await page.getByLabel("Identifiant").fill("admin");
  await page.getByLabel("Mot de passe").fill("x");
  await page.getByRole("button", { name: "Se connecter", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Mes fichiers" })).toBeVisible();
}

test("the trash lists deleted files and restores one", async ({ page }) => {
  const restored: string[] = [];
  await setup(page, restored);
  await page.getByLabel("État").selectOption({ label: "Corbeille" });

  await page.getByRole("button", { name: "Actions pour rapport.txt" }).click();
  await page.getByRole("menuitem", { name: "Restaurer" }).click();
  await expect(page.getByText("Fichier restauré depuis la corbeille.")).toBeVisible();
  expect(restored).toEqual(["t1"]);
});

test("a file deleted before its upload cannot be restored", async ({ page }) => {
  await setup(page, []);
  await page.getByLabel("État").selectOption({ label: "Corbeille" });
  await page.getByRole("button", { name: "Actions pour jamais-envoye.txt" }).click();
  await expect(page.getByRole("menuitem", { name: "Restaurer" })).toHaveCount(0);
});
