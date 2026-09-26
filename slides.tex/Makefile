.SUFFIXES: .tex .pdf

%.pdf: %.tex
	xelatex --shell-escape $?
	xelatex --shell-escape $?
	git add $@ $^

all: 01_0.C_Language_History.pdf

clean:
	-rm *.aux *.log *.nav *.out *.toc *.vrb *.snm *.synctex.gz
	-rm -rf _minted-* svg-inkscape

cleanall: clean
	-rm *.pdf
