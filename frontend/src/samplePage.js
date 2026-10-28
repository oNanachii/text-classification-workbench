// Refresh filtered data after mutations without moving the selected detail/draft.
const key = query => JSON.stringify({
  q: query.q,
  pending: query.pending,
  offset: query.offset,
  limit: query.limit,
})

export function createSamplePageLoader() {
  let generation = 0
  return {
    invalidate() { generation += 1 },
    async refresh(getQuery, fetchPage, commit) {
      const ticket = ++generation
      const initial = { ...getQuery() }
      let query = { ...initial }
      // GET-only range reconciliation; do not retry the mutation itself.
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const page = await fetchPage(query)
        if (ticket !== generation || key(getQuery()) !== key(initial)) return false
        const lastOffset = page.total > 0
          ? Math.floor((page.total - 1) / query.limit) * query.limit
          : 0
        const validOffset = Math.min(query.offset, lastOffset)
        if (validOffset !== query.offset) {
          query = { ...query, offset: validOffset }
          continue
        }
        commit({ ...page, offset: query.offset })
        return true
      }
      throw new Error('样本列表仍在变化，请重新查询；已确认的保存无需重做。')
    },
  }
}
