// Bind asynchronous import work to the exact visible draft and edit generation.
export function createImportGuard() {
  let generation = 0
  return {
    invalidate() { generation += 1 },
    ticket(csv, source) { return { generation, csv, source } },
    accepts(ticket, csv = ticket.csv, source = ticket.source) {
      return ticket.generation === generation && ticket.csv === csv && ticket.source === source
    },
  }
}

export async function guardedPreview(guard, ticket, request, currentDraft, commit) {
  const result = await request()
  const draft = currentDraft()
  if (!guard.accepts(ticket, draft.csv, draft.source)) return false
  commit(result)
  return true
}
