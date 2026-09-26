// spbu.typ — настройки для слайдов СПбГУ
// Шрифты лежат в res/fonts и подключаются через --font-path (см. Makefile).

#import "@preview/touying:0.5.5": *
#import "@preview/clean-math-presentation:0.1.1": *


#let terracotta = rgb("#9F2D20")
#let pantone    = rgb("#A8ADB4")

#let spbu-setup(body) = {
  set text(lang: "ru")
  set text(font: "Cuprum", size: 16pt)
  show raw: set text(font: "Iosevka NF", size: 14pt)
  show math.equation: set text(font: "New Computer Modern Math")
  body
}
