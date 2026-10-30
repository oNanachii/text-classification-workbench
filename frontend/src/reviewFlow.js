// Review intent and post-failure reconciliation. Same-label approval is never shown as a correction.
export function reviewIntent(sample, label, note = '') {
  if (!sample || (label !== 'ham' && label !== 'spam')) return { kind: 'none', needsNote: false, ready: false }
  const hasNote = Boolean(note.trim())
  if (label !== sample.label) return { kind: 'correct', needsNote: true, ready: hasNote }
  if (!sample.reviewed) return { kind: 'confirm', needsNote: true, ready: hasNote }
  return { kind: 'noop', needsNote: false, ready: false }
}

// After an unconfirmed save, compare a fresh server read with what was attempted.
//   not_saved: revision unchanged, saving again is safe.
//   matches:   revision advanced and the server label equals the draft; treat as saved, do not resubmit.
//   superseded: revision advanced with a different outcome; keep the draft and let the reviewer decide.
export function reconcileUnknownSave(attempt, server) {
  if (!attempt || !server) return 'superseded'
  if (server.revision === attempt.revision) return 'not_saved'
  if (server.revision > attempt.revision && server.label === attempt.label && server.reviewed) return 'matches'
  return 'superseded'
}

export function reviewStatus(row) {
  if (row.source?.startsWith('uci:') && row.revision === 0) return 'original'
  return row.reviewed ? 'reviewed' : 'pending'
}
