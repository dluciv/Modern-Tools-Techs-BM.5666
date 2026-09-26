// spbu.typ — настройки для слайдов СПбГУ
// Шрифты лежат в res/fonts и подключаются через --font-path (см. Makefile).

#import "@preview/touying:0.5.5": *
#import "@preview/clean-math-presentation:0.1.1": *


#let terracotta = rgb("#9F2D20")
#let pantone    = rgb("#A8ADB4")

// Круглые номера для нумерованных списков (как в Beamer)
#let _num-icon(n) = box(width: 19pt, height: 19pt)[
  #place(center + horizon, circle(radius: 9.5pt, fill: terracotta))
  #place(center + horizon, text(fill: white, size: 11pt, weight: "bold", str(n)))
]

#let spbu-setup(body) = {
  set text(lang: "ru")
  set text(font: "Cuprum", size: 16pt)
  show raw: set text(font: "Iosevka NF", size: 17.5pt)
  show math.equation: set text(font: "New Computer Modern Math")
  show enum: set enum(spacing: 10pt, numbering: n => move(dy: 0.35em, _num-icon(n)))
  body
}

// Обёртка над clean-math-presentation с настройками СПбГУ.
// Использование:
//   #show: spbu-theme.with(config-info(title: [...], author: [...]))
#let spbu-theme = clean-math-presentation-theme.with(
  config-colors(
    primary: terracotta,
    secondary: pantone,
  ),
  progress-bar: true,
)
