<script setup>
import { computed } from 'vue'

const props = defineProps({
  history: { type: Array, default: () => [] },
  name: { type: String, default: '' },
  checkpoint: { type: Number, default: null },
})
const plot = { left: 55, right: 540, top: 20, bottom: 190 }
const points = computed(() => props.history.filter(point => Number.isFinite(point.epoch)).slice().sort((a, b) => a.epoch - b.epoch))
const values = computed(() => points.value.flatMap(point => [point.train_loss, point.validation_loss]).filter(Number.isFinite))
const hasData = computed(() => values.value.length > 0)
const minEpoch = computed(() => points.value[0]?.epoch ?? 1)
const maxEpoch = computed(() => points.value.at(-1)?.epoch ?? 1)
const maxLoss = computed(() => Math.max(...values.value, 0.001) * 1.08)
const x = epoch => plot.left + ((epoch - minEpoch.value) / Math.max(1, maxEpoch.value - minEpoch.value)) * (plot.right - plot.left)
const y = loss => plot.bottom - (loss / maxLoss.value) * (plot.bottom - plot.top)
const yTicks = computed(() => [0, maxLoss.value / 2, maxLoss.value])
const xTicks = computed(() => [...new Set([minEpoch.value, Math.round((minEpoch.value + maxEpoch.value) / 2), maxEpoch.value])])
function path(key) {
  let connected = false
  return points.value.map(point => {
    if (!Number.isFinite(point[key])) { connected = false; return '' }
    const command = connected ? 'L' : 'M'
    connected = true
    return `${command}${x(point.epoch).toFixed(2)},${y(point[key]).toFixed(2)}`
  }).join(' ')
}
const checkpointVisible = computed(() => Number.isFinite(props.checkpoint) && props.checkpoint >= minEpoch.value && props.checkpoint <= maxEpoch.value)
</script>

<template>
  <figure class="loss-chart">
    <div v-if="!hasData" class="chart-empty"><p>暂无可绘制的训练记录。</p></div>
    <template v-else>
      <div class="chart-legend"><span><i class="legend-line train"></i>训练损失</span><span><i class="legend-line validation"></i>验证损失</span></div>
      <svg viewBox="0 0 560 230" role="img" :aria-label="`${name}的训练与验证损失随轮次变化曲线`">
        <g v-for="tick in yTicks" :key="tick"><line :x1="plot.left" :x2="plot.right" :y1="y(tick)" :y2="y(tick)" class="chart-grid-line" /><text :x="plot.left - 10" :y="y(tick) + 4" text-anchor="end" class="chart-tick">{{ tick.toFixed(3) }}</text></g>
        <line :x1="plot.left" :x2="plot.right" :y1="plot.bottom" :y2="plot.bottom" class="chart-axis" />
        <g v-for="tick in xTicks" :key="tick"><text :x="x(tick)" :y="plot.bottom + 20" text-anchor="middle" class="chart-tick">{{ tick }}</text></g>
        <line v-if="checkpointVisible" :x1="x(checkpoint)" :x2="x(checkpoint)" :y1="plot.top" :y2="plot.bottom" class="chart-checkpoint" />
        <path :d="path('train_loss')" class="chart-line train" />
        <path :d="path('validation_loss')" class="chart-line validation" />
        <text x="298" y="226" text-anchor="middle" class="chart-tick">训练轮次（epoch）</text>
      </svg>
      <figcaption>虚线标记保留的检查点：{{ Number.isFinite(checkpoint) ? `第${checkpoint}轮` : '—' }}。损失越低，说明在对应数据上的预测误差越小。</figcaption>
    </template>
  </figure>
</template>
