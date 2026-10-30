<script setup>
import Icon from './Icon.vue'
defineProps({ publicMode: Boolean, what: { type: String, required: true } })
</script>

<template>
  <div class="state-card">
    <span class="state-icon"><Icon name="lock" /></span>
    <h2>{{ publicMode ? `公开只读部署不提供${what}` : '需要操作者身份' }}</h2>
    <p v-if="publicMode">公开部署不展示私有样本、导入文件或纠错历史；即使请求带有操作者令牌，服务器也拒绝写入。{{ what }}只在操作者的本地工作台进行。</p>
    <p v-else>点右上角“操作者登录”载入令牌后可用。令牌只保存在当前页面内存中，不写入浏览器存储。</p>
  </div>
</template>
