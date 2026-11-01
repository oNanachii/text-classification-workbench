<script setup>
import { ref } from 'vue'
import Icon from './Icon.vue'
import { browserStorage, guideClosed, rememberGuideClosed } from '../firstVisit.js'

defineProps({ sections: { type: Array, required: true } })
const emit = defineEmits(['close', 'navigate'])

// One line per navigation section; titles and order come from the navigation itself.
const PURPOSE = {
  decide: '单条判断：本地、Jev 或并排对照',
  import: '导入 CSV，预览检查后再进待审',
  review: '逐条复核标签，记录依据与修订',
  evidence: '分开展示验证、最终测试与 Jev 现状',
  model: '实际加载的模型、已选候选与规则',
}
const open = ref(!guideClosed(browserStorage()))

function close() {
  rememberGuideClosed(browserStorage())
  open.value = false
  emit('close')
}
</script>

<template>
  <section v-if="open" class="first-visit" aria-labelledby="first-visit-title" @keydown.esc.stop="close">
    <div class="fv-head">
      <div>
        <h2 id="first-visit-title" class="fv-kicker">第一次来？看这里</h2>
        <p class="fv-lede">英文垃圾短信识别，以及背后的数据审核和模型版本管理。</p>
      </div>
      <button type="button" class="icon-btn" aria-label="关闭首次使用说明" title="关闭说明（Esc）" @click="close"><Icon name="x" /></button>
    </div>
    <div class="fv-grid">
      <div class="fv-block">
        <h3 class="fv-label">怎么试</h3>
        <ol class="fv-steps">
          <li><span class="step-no">1</span><span>点任一“自编示例”按钮，填入一条英文短信。</span></li>
          <li><span class="step-no">2</span><span>点“本地判断”，查看 ham / spam 建议和分数。</span></li>
        </ol>
        <p class="fine">默认使用本地模型：不外发文本，也不需要 Key。</p>
      </div>
      <div class="fv-block">
        <h3 class="fv-label">各功能区</h3>
        <dl class="fv-sections">
          <div v-for="section in sections" :key="section.id" class="fv-section">
            <dt><a :href="'#' + section.id" @click.prevent="emit('navigate', section.id)">{{ section.title }}</a></dt>
            <dd>{{ PURPOSE[section.id] }}</dd>
          </div>
        </dl>
        <p class="fine">导入和审核需要操作者身份，公开演示中已锁定。</p>
      </div>
    </div>
  </section>
</template>
