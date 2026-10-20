import { test, expect } from "@playwright/test";
import type { Page, Route } from "@playwright/test";

/**
 * Inline preview. The API is fully mocked (no object is created) and the
 * presigned preview URL points at a local route, so S3 is never contacted.
 */

function item(id: string, name: string, type: string) {
  return {
    id, display_name: name, content_type: type, size_bytes: 1234,
    status: "available", folder_id: null,
    checksum_sha256: "a".repeat(64), created_at: 1_700_000_000, updated_at: 1_700_000_000,
  };
}

const ITEMS = [
  item("img-1", "plan.png", "image/png"),
  item("pdf-1", "contrat.pdf", "application/pdf"),
  item("html-1", "page.html", "text/html"),
  item("bad-1", "forcee.png", "image/png"),
];

async function setup(page: Page) {
  const PNG = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
    "base64",
  );
  await page.route("**/fake-s3/**", (route) =>
    route.fulfill({ status: 200, contentType: "image/png", body: PNG }),
  );
  await page.route("**/api/v1/**", async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path === "/api/v1/login") {
      await route.fulfill({ json: { csrf_token: "csrf-test" } });
    } else if (path === "/api/v1/me") {
      await route.fulfill({
        json: {
          id: "u1", kind: "user", name: "admin", role: "admin", space_id: "s1",
          application_id: "a1", scopes: ["objects:read"], csrf_token: "csrf-test",
        },
      });
    } else if (path === "/api/v1/folders") {
      await route.fulfill({ json: { items: [] } });
    } else if (path === "/api/v1/objects" && method === "GET") {
      await route.fulfill({ json: { items: ITEMS } });
    } else if (path.endsWith("/preview-intent")) {
      const id = path.split("/").slice(-2)[0];
      if (id === "bad-1") {
        await route.fulfill({
          status: 415,
          json: { detail: "preview not supported for this content type" },
        });
        return;
      }
      const found = ITEMS.find((i) => i.id === id)!;
      await route.fulfill({
        json: {
          id, preview_url: `/fake-s3/${id}`, method: "GET", expires_in: 300,
          expires_at: null, display_name: found.display_name,
          content_type: found.content_type, size_bytes: found.size_bytes,
        },
      });
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

async function openMenu(page: Page, name: string) {
  await page.getByRole("button", { name: `Actions pour ${name}` }).click();
}

test("an image opens in the preview as an <img>", async ({ page }) => {
  await setup(page);
  await openMenu(page, "plan.png");
  await page.getByRole("menuitem", { name: "Aperçu" }).click();
  const dialog = page.getByRole("dialog", { name: /Aperçu — plan\.png/ });
  await expect(dialog.locator("img.preview-media")).toHaveAttribute("src", "/fake-s3/img-1");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("a PDF opens in a iframe pointing at the preview URL", async ({ page }) => {
  await setup(page);
  await openMenu(page, "contrat.pdf");
  await page.getByRole("menuitem", { name: "Aperçu" }).click();
  const dialog = page.getByRole("dialog", { name: /Aperçu — contrat\.pdf/ });
  await expect(dialog.locator("iframe.preview-frame")).toHaveAttribute("src", "/fake-s3/pdf-1");
});

test("a type outside the allow-list has no Aperçu entry", async ({ page }) => {
  await setup(page);
  await openMenu(page, "page.html");
  await expect(page.getByRole("menuitem", { name: "Télécharger" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Aperçu" })).toHaveCount(0);
});

test("a 415 from the API shows a clear message", async ({ page }) => {
  await setup(page);
  await openMenu(page, "forcee.png");
  await page.getByRole("menuitem", { name: "Aperçu" }).click();
  await expect(
    page.getByRole("dialog", { name: /Aperçu — forcee\.png/ }).getByRole("alert"),
  ).toContainText("ne peut pas être affiché");
});

test("the details panel offers Aperçu for a previewable file", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Détails de plan.png" }).click();
  await page.getByRole("dialog", { name: "plan.png" }).getByRole("button", { name: "Aperçu" }).click();
  await expect(page.locator("img.preview-media")).toBeVisible();
});
