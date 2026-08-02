/*
 * The languages the picker offers, and the name to show for a code.
 *
 * **Why this list exists at all.** Auto-detect is the default and is right most of the time,
 * but it cannot be relied on. A real 2.76-second Romanian memo recorded into this app came
 * back transliterated into Cyrillic, and the cause was measured rather than guessed: nine
 * language-ID approaches were run against that one clip and every one of them answered Slavic
 * or Baltic.
 *
 *   whisper tiny  `en` 0.14      VoxLingua107 ECAPA    `lt` 0.99
 *   whisper base  `el` 0.39      Meta MMS-LID-256      `rus` 0.98
 *   whisper small `lt` 0.63      CommonLanguage ECAPA  no signal
 *   whisper medium `lt` 0.23     avg_logprob rescoring `uk`
 *   whisper large-v3-turbo `ru` 0.19
 *
 * Three unrelated architectures, trained on different corpora, two of them confidently wrong.
 * All of them identify clean Romanian and the same speaker's Russian correctly, so the models
 * are not the problem: sub-3-second language ID is a known limit. Whisper's own API exposes a
 * `language` parameter for the same reason, and every shipping dictation product asks. This is
 * that ask. 005_memo_language.sql has the long version.
 *
 * **A subset, not all 99.** The API accepts every code Whisper knows -- see
 * App\Http\Rules\SupportedLanguage -- and this list is only what the dropdown shows, because a
 * 99-item select is a worse control than a 30-item one for a choice most people make once. The
 * ordering is by number of speakers rather than alphabetical: the point of a picker is that the
 * language you want is near the top, and alphabetical puts Afrikaans there.
 *
 * Endonyms beside the English name -- "Русский", "中文" -- because somebody looking for their
 * own language scans for the word they call it, not for what English calls it.
 */

/** The value meaning "work it out from the audio". Empty string, so it is a falsy `<option>`. */
export const AUTO_DETECT = ''

/**
 * @type {ReadonlyArray<{code: string, name: string}>}
 */
export const LANGUAGES = Object.freeze([
  { code: 'en', name: 'English' },
  { code: 'zh', name: '中文 · Chinese' },
  { code: 'hi', name: 'हिन्दी · Hindi' },
  { code: 'es', name: 'Español · Spanish' },
  { code: 'ar', name: 'العربية · Arabic' },
  { code: 'fr', name: 'Français · French' },
  { code: 'pt', name: 'Português · Portuguese' },
  { code: 'ru', name: 'Русский · Russian' },
  { code: 'de', name: 'Deutsch · German' },
  { code: 'ja', name: '日本語 · Japanese' },
  { code: 'ko', name: '한국어 · Korean' },
  { code: 'it', name: 'Italiano · Italian' },
  { code: 'tr', name: 'Türkçe · Turkish' },
  { code: 'pl', name: 'Polski · Polish' },
  { code: 'uk', name: 'Українська · Ukrainian' },
  { code: 'ro', name: 'Română · Romanian' },
  { code: 'nl', name: 'Nederlands · Dutch' },
  { code: 'vi', name: 'Tiếng Việt · Vietnamese' },
  { code: 'id', name: 'Indonesia · Indonesian' },
  { code: 'th', name: 'ไทย · Thai' },
  { code: 'el', name: 'Ελληνικά · Greek' },
  { code: 'cs', name: 'Čeština · Czech' },
  { code: 'hu', name: 'Magyar · Hungarian' },
  { code: 'sv', name: 'Svenska · Swedish' },
  { code: 'da', name: 'Dansk · Danish' },
  { code: 'fi', name: 'Suomi · Finnish' },
  { code: 'no', name: 'Norsk · Norwegian' },
  { code: 'he', name: 'עברית · Hebrew' },
  { code: 'fa', name: 'فارسی · Persian' },
  { code: 'bg', name: 'Български · Bulgarian' },
  { code: 'sr', name: 'Српски · Serbian' },
  { code: 'hr', name: 'Hrvatski · Croatian' },
  { code: 'sk', name: 'Slovenčina · Slovak' },
  { code: 'ca', name: 'Català · Catalan' },
])

/*
 * There is deliberately no `languageName(code)` helper here.
 *
 * One existed while MemoDialog named a memo's language in the hint beside a select that
 * decoded it again. That select is gone -- a transcript is now corrected by editing it
 * rather than by re-running the model -- so nothing renders a stored code, and a lookup
 * with no caller is a thing that rots. It is a five-line Map over LANGUAGES if a reason
 * to display one comes back.
 */
