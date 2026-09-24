import test from "node:test";
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";
import {
  address,
  createSolanaRpc,
  createKeyPairFromBytes,
  signTransaction,
  compileTransaction,
  getBase64EncodedWireTransaction,
  compressTransactionMessageUsingAddressLookupTables,
  setTransactionMessageComputeUnitLimit,
  setTransactionMessageLoadedAccountsDataSizeLimit,
} from "@solana/kit";
import {
  getAddressLookupTableEncoder,
  fetchAddressLookupTable,
} from "@solana-program/address-lookup-table";
import {
  buildTransferBatch,
  FIXTURE_RECIPIENT,
  FIXTURE_TABLE,
} from "../../src/builder.js";
import { estimateResources } from "../../src/runtime.js";
import {
  type BuilderMessage,
  type LookupEvidence,
  bindMessage,
  canonical,
  verifyBoundMessage,
} from "../../src/message.js";

test(
  "real Surfpool: Kit builder -> bound fallback -> local signed execution (legacy/v0/v1)",
  { timeout: 120000 },
  async () => {
    let Surfnet;
    try {
      ({ Surfnet } = await import(
        pathToFileURL(resolve("../tests/integration/runtime/start.mjs")).href
      ));
    } catch {
      throw new Error(
        "INCOMPLETE INTEGRATION: install tests/integration/runtime with npm ci on Linux x64 or macOS; Windows requires WSL. Surfpool native prerequisite unavailable.",
      );
    }
    const surf = Surfnet.startWithConfig({
      offline: true,
      allFeatures: true,
      blockProductionMode: "transaction",
    });
    const reports: unknown[] = [];
    try {
      const rpc = createSolanaRpc(surf.rpcUrl);
      const payer = address(surf.payer);
      const key = await createKeyPairFromBytes(
        Uint8Array.from(surf.payerSecretKey),
      );
      surf.fundSol(FIXTURE_RECIPIENT, 1_000_000);
      const versionInfo = await rpc.getVersion().send();
      for (const version of ["legacy", 0, 1] as const) {
        surf.timeTravelToSlot(100 + reports.length * 10);
        const currentSlot = await rpc
          .getSlot({ commitment: "confirmed" })
          .send();
        const { value: lifetime } = await rpc
          .getLatestBlockhash({ commitment: "confirmed" })
          .send();
        let message = buildTransferBatch({
          version,
          payer,
          destinations: [FIXTURE_RECIPIENT, FIXTURE_RECIPIENT],
          amounts: [1000000n, 2000000n],
          blockhash: lifetime.blockhash,
          lastValidBlockHeight: lifetime.lastValidBlockHeight,
        });
        let lookup: LookupEvidence | undefined;
        if (message.version === 0) {
          const data = getAddressLookupTableEncoder().encode({
            deactivationSlot: 2n ** 64n - 1n,
            lastExtendedSlot: currentSlot - 1n,
            lastExtendedSlotStartIndex: 0,
            authority: null,
            addresses: [FIXTURE_RECIPIENT],
          });
          surf.setAccount(
            FIXTURE_TABLE,
            10_000_000,
            [...data],
            "AddressLookupTab1e1111111111111111111111111",
          );
          const table = await fetchAddressLookupTable(rpc, FIXTURE_TABLE, {
            commitment: "confirmed",
            minContextSlot: currentSlot,
          });
          assert.equal(
            table.programAddress,
            "AddressLookupTab1e1111111111111111111111111",
          );
          lookup = {
            tables: { [FIXTURE_TABLE]: table.data.addresses },
            checkedSlot: currentSlot,
            cluster: "local-surfpool",
          };
          message = compressTransactionMessageUsingAddressLookupTables(
            message,
            lookup.tables,
          ) as BuilderMessage;
        }
        // Deliberately insufficient original requests are replaced before actual simulation.
        message = setTransactionMessageLoadedAccountsDataSizeLimit(
          1,
          setTransactionMessageComputeUnitLimit(1, message),
        );
        const result = await estimateResources(message, {
          rpc,
          currentSlot,
          cluster: "local-surfpool",
          runtime: "surfpool-1.5.0",
          context: "local:batch",
          workload: "system-transfer-batch",
          budgetIndependent: true,
          lookup,
          shadow: true,
        });
        assert.equal(result.status, "simulation", canonical(result));
        assert.ok(result.simulation!.computeUnits! > 0);
        assert.ok(result.simulation!.loadedAccountsBytes! > 0);
        assert.equal(
          result.final!.features.pattern_id,
          result.prepared!.features.pattern_id,
        );
        assert.notEqual(
          result.original!.messageIdentity,
          result.prepared!.messageIdentity,
        );
        assert.equal(
          verifyBoundMessage(result.final!, result.unsignedMessage!, lookup),
          true,
        );
        const signed = await signTransaction(
          [key],
          compileTransaction(result.unsignedMessage!),
        );
        const signature = await rpc
          .sendTransaction(getBase64EncodedWireTransaction(signed), {
            encoding: "base64",
            skipPreflight: false,
            preflightCommitment: "confirmed",
          })
          .send();
        const execution = await rpc
          .getTransaction(signature, {
            encoding: "json",
            commitment: "confirmed",
            maxSupportedTransactionVersion: 1,
          })
          .send();
        assert.ok(execution);
        assert.equal(execution.meta!.err, null);
        assert.ok(
          execution.meta!.computeUnitsConsumed! <=
            BigInt(result.limits!.computeUnits),
        );
        reports.push({
          version,
          simulation: result.simulation,
          limits: result.limits,
          executionComputeUnits: execution.meta!.computeUnitsConsumed,
          totalPreparationMs: result.timings.totalMs,
          wireBytes: result.final!.transaction.serialized_size,
          signatureVerifiedByLocalRuntime: true,
        });
        const mismatch = await estimateResources(message, {
          rpc,
          currentSlot,
          cluster: "local-surfpool",
          runtime: "surfpool-1.5.0",
          context: "local:batch",
          workload: "system-transfer-batch",
          budgetIndependent: true,
          lookup,
          expectedWire: new Uint8Array([1]),
        });
        assert.equal(mismatch.reason, "message_wire_mismatch");
        if (lookup) {
          const stale = await estimateResources(message, {
            rpc,
            currentSlot: currentSlot + 151n,
            cluster: "local-surfpool",
            runtime: "surfpool-1.5.0",
            context: "local:batch",
            workload: "system-transfer-batch",
            budgetIndependent: true,
            lookup,
          });
          assert.equal(stale.reason, "stale_lookup_evidence");
        }
      }
      // A real deterministic failure must not turn partial consumption into successful demand.
      const { value: lifetime } = await rpc.getLatestBlockhash().send();
      const currentSlot = await rpc.getSlot().send();
      const impossible = buildTransferBatch({
        version: "legacy",
        payer,
        destinations: [FIXTURE_RECIPIENT],
        amounts: [2n ** 63n],
        blockhash: lifetime.blockhash,
        lastValidBlockHeight: lifetime.lastValidBlockHeight,
      });
      const failed = await estimateResources(impossible, {
        rpc,
        currentSlot,
        cluster: "local-surfpool",
        runtime: "surfpool-1.5.0",
        context: "local:batch",
        workload: "system-transfer-batch",
        budgetIndependent: true,
      });
      assert.equal(failed.status, "unresolved");
      assert.equal(failed.reason, "transaction_error");
      assert.equal(failed.simulation!.attempts, 1);
      const controller = new AbortController();
      controller.abort();
      const cancelled = await estimateResources(impossible, {
        rpc,
        currentSlot,
        cluster: "local-surfpool",
        runtime: "surfpool-1.5.0",
        context: "local:batch",
        workload: "system-transfer-batch",
        budgetIndependent: true,
        abortSignal: controller.signal,
      });
      assert.equal(cancelled.reason, "cancelled");
      const report = {
        evidence: "local-runtime",
        node: process.version,
        platform: process.platform,
        kit: "8.3.0",
        surfpool: "1.5.0",
        runtime: versionInfo,
        cases: reports,
        limits:
          "Local controlled workload only; no production reliability or latency claim.",
      };
      writeFileSync(
        ".integration-report.json",
        JSON.stringify(JSON.parse(canonical(report)), null, 2) + "\n",
      );
      console.log(canonical(report));
    } finally {
      surf.stop();
    }
  },
);
