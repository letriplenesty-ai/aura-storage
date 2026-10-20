import { test, expect } from "@playwright/test";
import { ApiError } from "../src/lib/api";
import { classifyError, runTransfer } from "../src/lib/transfer";
import type { TransferDeps } from "../src/lib/transfer";

/** A full store (507) is final: no retry loop, and a clear French message. */

test("507 is not retried and explains that the storage is full", async () => {
  expect(classifyError(new ApiError(507, "storage capacity reached"))).toBe("fatal");
  let intents = 0;
  const deps: TransferDeps = {
    requestIntent: async () => {
      intents += 1;
      throw new ApiError(507, "storage capacity reached");
    },
    upload: async () => undefined,
    commit: async () => ({ id: "x", status: "available", size_bytes: 1, checksum_sha256: "" }),
    abortIntent: async () => undefined,
    sleep: async () => undefined,
    random: () => 0,
  };
  const file = new File([new Uint8Array(1)], "a.bin");
  const result = await runTransfer(file, deps, { folderId: null, idempotencyKey: "k" });
  expect(result.state.phase).toBe("failed");
  expect(result.state.error).toContain("Espace de stockage plein");
  expect(intents).toBe(1);
});
