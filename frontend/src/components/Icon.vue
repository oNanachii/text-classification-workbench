<script setup>
// Inline stroke icons; no external icon font or remote asset (CSP: self only).
defineProps({ name: { type: String, required: true } })
const PATHS = {
  decide: ['M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4h-.5A1 1 0 0 1 4 15z', 'M8.5 8.5h7', 'M8.5 12h4.5'],
  import: ['M12 15V4', 'M8 8l4-4 4 4', 'M4 14v3.5A2.5 2.5 0 0 0 6.5 20h11a2.5 2.5 0 0 0 2.5-2.5V14'],
  review: ['M7 4h10a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z', 'M8.5 12.5l2.5 2.5 4.5-5'],
  evidence: ['M4 20h16', 'M6.5 20v-8', 'M12 20V5', 'M17.5 20v-5'],
  model: ['M12 3.5l8.5 4.5-8.5 4.5L3.5 8z', 'M3.5 12.5l8.5 4.5 8.5-4.5', 'M3.5 16.5L12 21l8.5-4.5'],
  lock: ['M6.5 11h11a1.5 1.5 0 0 1 1.5 1.5v6a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 18.5v-6A1.5 1.5 0 0 1 6.5 11z', 'M8.5 11V8a3.5 3.5 0 0 1 7 0v3'],
  key: ['M14.5 9.5a4 4 0 1 1-8 0 4 4 0 0 1 8 0z', 'M13.5 12.5L20 19', 'M17 16l2-2'],
  shield: ['M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z', 'M9 12l2 2 4-4'],
  alert: ['M10.3 4.4L2.8 17.5A2 2 0 0 0 4.5 20.5h15a2 2 0 0 0 1.7-3L13.7 4.4a2 2 0 0 0-3.4 0z', 'M12 10v4', 'M12 17.2v.01'],
  check: ['M5 12.5l4.5 4.5L19 7.5'],
  info: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 11v5', 'M12 7.8v.01'],
  x: ['M6.5 6.5l11 11', 'M17.5 6.5l-11 11'],
  arrow: ['M5 12h14', 'M13 6l6 6-6 6'],
  refresh: ['M19.5 12a7.5 7.5 0 1 1-2.2-5.3', 'M19.5 4.5v4h-4'],
  split: ['M12 4v16', 'M4 7h5', 'M4 12h5', 'M15 7h5', 'M15 12h5', 'M4 17h5', 'M15 17h5'],
  clock: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 7.5V12l3 2'],
  file: ['M7 3.5h6.5L18 8v11a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19V5a1.5 1.5 0 0 1 1-1.5z', 'M13 3.5V8h5'],
}
</script>

<template>
  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path v-for="d in PATHS[name] || []" :key="d" :d="d" /></svg>
</template>
