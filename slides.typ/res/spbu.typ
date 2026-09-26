// spbu.typ — настройки для слайдов СПбГУ
// Шрифты лежат в res/fonts и подключаются через --font-path (см. Makefile).

#import "@preview/touying:0.5.5": *
#import "@preview/clean-math-presentation:0.1.1": *

#let terracotta  = rgb("#9F2D20")
#let pantone     = rgb("#A8ADB4")
#let block-bg    = rgb("#F2F2F2")

// Номер в кружке — центр кружка по центру строки
#let _num-icon(n) = {
  let r = 9pt
  box(width: 2 * r, height: 2 * r, baseline: 20%)[
    #place(center + horizon, circle(radius: r, fill: terracotta))
    #place(center + horizon, text(fill: white, size: 14pt, weight: "bold", str(n)))
  ]
}

// Кружок-маркер для маркированного списка
#let _bullet-icon = {
  let r = 3pt
  box(width: 2 * r, height: 2 * r, baseline: -15%)[
    #place(center + horizon, circle(radius: r, fill: terracotta))
  ]
}

// Блок с серым фоном (перекрывает tblock из темы)
#let tblock(title: none, body) = {
  grid(columns: 1, row-gutter: 0pt,
    block(fill: terracotta, width: 100%, radius: (top: 6pt),
      inset: (top: 0.4em, bottom: 0.3em, left: 0.5em, right: 0.5em),
      text(fill: white, weight: "bold", title)),
    rect(fill: gradient.linear(terracotta, terracotta.lighten(90%), angle: 90deg),
      width: 100%, height: 4pt),
    block(fill: block-bg, width: 100%, radius: (bottom: 6pt),
      inset: (top: 0.4em, bottom: 0.5em, left: 0.5em, right: 0.5em), body),
  )
}

#let spbu-setup(body) = {
  set text(lang: "ru")
  set text(font: "Cuprum", size: 18pt)
  show raw: set text(font: "Iosevka NF", size: 17.5pt)
  show math.equation: set text(font: "New Computer Modern Math")
  show enum: set enum(spacing: 10pt, numbering: n => _num-icon(n))
  show list: set list(marker: _bullet-icon)
  body
}

// Обёртка над clean-math-presentation с настройками СПбГУ.
#let spbu-theme = clean-math-presentation-theme.with(
  config-colors(
    primary: terracotta,
    secondary: pantone,
  ),
  config-store(
    navigation: self => {
      context {
        let current-heading = utils.current-heading(level: 1)
        let current-heading-name = if current-heading != none {
          current-heading.body
        } else { "" }

        grid(
          columns: (1fr, 1fr),
          block(
            width: 100%, height: 0.8em,
            fill: self.colors.primary,
            place(left + horizon, text(current-heading-name, fill: self.colors.neutral-lightest, size: 0.7em), dx: 0.3em),
          ),
          block(
            width: 100%, height: 0.8em,
            fill: self.colors.primary,
          ),
        )
      }
    },
  ),
  progress-bar: true,
)

#let dia(c) = { c + "\u{0308}" }
