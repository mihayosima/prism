/**
 * asr.logic 单测（阶段 2）
 * ------------------------------------------------------------------
 * 为什么测这个文件：降级矩阵是**错了就静默失效**的那类逻辑——
 * 选错策略不会报错，只会"语音按钮点了没反应"或"悄悄把音频发到云端"。
 * 本仓无 DOM 测试库（未装 jsdom/happy-dom），故本文件全部测**纯函数**。
 *
 * 覆盖重点（按"错了的代价"排序）：
 *   1. 降级矩阵：本地 → Web Speech → 纯文本，顺序不可换；
 *   2. Web Speech 探测：两个实现名 + 非浏览器环境；
 *   3. final 拼接：partial **不**进草稿（它是会被覆盖的实时改写）；
 *   4. 帧守卫：脏数据不得进状态机；
 *   5. 时间格式化。
 */

import { describe, it, expect } from 'vitest'
import {
  pickStrategy,
  strategyNotice,
  isSpeechApiAvailable,
  concatFinals,
  formatDuration,
  isAsrFrame,
  isEngineServable,
} from './asr.logic'

describe('pickStrategy 降级矩阵（顺序即优先级）', () => {
  it('引擎就绪 + WS 可用 ⇒ 本地（隐私最优）', () => {
    expect(pickStrategy({ wsSupported: true, webSpeechSupported: true, engineReady: true })).toBe('local')
  })

  it('引擎未就绪但浏览器支持 ⇒ 降级 Web Speech（不返回 local）', () => {
    expect(pickStrategy({ wsSupported: true, webSpeechSupported: true, engineReady: false })).toBe('webspeech')
  })

  it('WS 不可用但引擎"就绪"⇒ 也不能选 local（连不上就是连不上）', () => {
    expect(pickStrategy({ wsSupported: false, webSpeechSupported: true, engineReady: true })).toBe('webspeech')
  })

  it('两条路都不可用 ⇒ 纯文本（明说不可用，不假装）', () => {
    expect(pickStrategy({ wsSupported: true, webSpeechSupported: false, engineReady: false })).toBe('text')
    expect(pickStrategy({ wsSupported: false, webSpeechSupported: false, engineReady: true })).toBe('text')
  })

  it('Web Speech 不可用时，即使引擎未就绪也不得返回 webspeech', () => {
    expect(pickStrategy({ wsSupported: true, webSpeechSupported: false, engineReady: false })).not.toBe('webspeech')
  })
})

describe('strategyNotice 诚实性', () => {
  it('切到 Web Speech 时**必须**告知音频将出本机（本地私有承诺）', () => {
    const n = strategyNotice('webspeech', '未安装 sherpa-onnx')
    expect(n).toContain('云端')
    expect(n).toContain('未安装 sherpa-onnx') // 附带真实原因，便于排障
  })

  it('本地模式声明"不出本机"', () => {
    expect(strategyNotice('local')).toContain('不出本机')
  })

  it('纯文本模式明确指引用文字输入', () => {
    expect(strategyNotice('text')).toContain('文字输入')
  })
})

describe('isSpeechApiAvailable 两个实现名', () => {
  it('webkit 前缀（Chrome/Edge）', () => {
    expect(isSpeechApiAvailable({ webkitSpeechRecognition: function () {} })).toBe(true)
  })
  it('标准名（部分实现）', () => {
    expect(isSpeechApiAvailable({ SpeechRecognition: function () {} })).toBe(true)
  })
  it('两者都无 ⇒ false', () => {
    expect(isSpeechApiAvailable({})).toBe(false)
  })
  it('非浏览器环境（undefined/null）⇒ false，不得抛错', () => {
    expect(isSpeechApiAvailable(undefined)).toBe(false)
    expect(isSpeechApiAvailable(null)).toBe(false)
  })
  it('值非函数（脏 polyfill）⇒ 不得误判为可用', () => {
    expect(isSpeechApiAvailable({ webkitSpeechRecognition: {} })).toBe(false)
  })
})

describe('concatFinals 草稿累积', () => {
  it('只拼 final（partial 是会被覆盖的实时改写，不得入草稿）', () => {
    expect(concatFinals(['今天', '天气', '怎么样'])).toBe('今天天气怎么样')
  })
  it('空数组 ⇒ 空串', () => {
    expect(concatFinals([])).toBe('')
  })
})

describe('isAsrFrame 帧守卫（脏数据不进状态机）', () => {
  it('接受后端实际发的五种帧', () => {
    expect(isAsrFrame({ type: 'loading' })).toBe(true)
    expect(isAsrFrame({ type: 'ready' })).toBe(true)
    expect(isAsrFrame({ type: 'partial', text: 'a' })).toBe(true)
    expect(isAsrFrame({ type: 'final', text: 'a' })).toBe(true)
    expect(isAsrFrame({ type: 'error', error: 'x' })).toBe(true)
  })
  it('拒绝未知类型/缺 type/非对象', () => {
    expect(isAsrFrame({ type: 'whatever' })).toBe(false)
    expect(isAsrFrame({})).toBe(false)
    expect(isAsrFrame(null)).toBe(false)
    expect(isAsrFrame('ready')).toBe(false)
  })
})

describe('formatDuration', () => {
  it('秒级补零', () => {
    expect(formatDuration(9)).toBe('00:09')
    expect(formatDuration(75)).toBe('01:15')
  })
  it('超过一小时带小时位', () => {
    expect(formatDuration(3661)).toBe('1:01:01')
  })
  it('负数/NaN 归零（不得显示 -1:-1）', () => {
    expect(formatDuration(-5)).toBe('00:00')
    expect(formatDuration(NaN)).toBe('00:00')
  })
})

describe('isEngineServable', () => {
  it('仅 ready 可服务；idle 需先触发加载，不算就绪', () => {
    expect(isEngineServable('ready')).toBe(true)
    expect(isEngineServable('idle')).toBe(false)
    expect(isEngineServable('loading')).toBe(false)
    expect(isEngineServable('unavailable')).toBe(false)
    expect(isEngineServable(undefined)).toBe(false)
  })
})
