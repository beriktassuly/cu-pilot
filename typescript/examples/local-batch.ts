import { pathToFileURL } from "node:url";
import { resolve } from "node:path";
import { address, createSolanaRpc } from "@solana/kit";
import { buildTransferBatch, FIXTURE_RECIPIENT } from "../src/builder.js";
import { estimateResources } from "../src/runtime.js";
import { canonical } from "../src/message.js";

// The native runtime is development-only; production applications supply their own rpc.
const { Surfnet } = await import(
  pathToFileURL(resolve("../tests/integration/runtime/start.mjs")).href
);
const local = Surfnet.startWithConfig({ offline: true, allFeatures: true });
try {
  const rpc = createSolanaRpc(local.rpcUrl);
  local.fundSol(FIXTURE_RECIPIENT, 1_000_000);
  const { value: lifetime } = await rpc.getLatestBlockhash().send();
  const message = buildTransferBatch({
    version: 0,
    payer: address(local.payer),
    destinations: [FIXTURE_RECIPIENT, FIXTURE_RECIPIENT],
    amounts: [1000000n, 2000000n],
    blockhash: lifetime.blockhash,
    lastValidBlockHeight: lifetime.lastValidBlockHeight,
  });
  const decision = await estimateResources(message, {
    rpc,
    currentSlot: await rpc.getSlot().send(),
    cluster: "local-surfpool",
    runtime: "surfpool-1.5.0",
    context: "local:batch",
    workload: "system-transfer-batch",
    budgetIndependent: true,
    shadow: true,
  });
  console.log(
    JSON.stringify(
      JSON.parse(
        canonical({
          evidence: "local-runtime",
          status: decision.status,
          reason: decision.reason,
          observationId: decision.observationId,
          originalIdentity: decision.original?.messageIdentity,
          simulationIdentity: decision.prepared?.messageIdentity,
          finalIdentity: decision.final?.messageIdentity,
          limits: decision.limits,
          simulation: decision.simulation,
          unsignedTransaction: decision.final?.wireBase64,
          preflightPolicy: decision.preflightPolicy,
          timings: decision.timings,
        }),
      ),
      null,
      2,
    ),
  );
  if (decision.status === "unresolved") process.exitCode = 2;
} finally {
  local.stop();
}
