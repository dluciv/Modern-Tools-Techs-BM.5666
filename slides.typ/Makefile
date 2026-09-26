FONT_PATH := res/fonts
PDFs      := 01_0.C_Language_History.pdf

.PHONY: all clean

all: $(PDFs)

%.pdf: %.typ
	typst compile --font-path $(FONT_PATH) $? $@

clean:
	echo Nothing to clean yet

cleanall:
	rm -f *.pdf
