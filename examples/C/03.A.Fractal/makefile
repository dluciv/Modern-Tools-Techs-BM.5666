CC=cc
LD=$(CC)

all: release debug debug_test

CCOPT_R += -O3 -DNDEBUG
CCOPT_D += -g -DDEBUG

release    : main.r.o      image.r.o fractal.r.o
	$(LD) -o $@ $^

debug      : main.d.o      image.d.o fractal.d.o
	$(LD) -o $@ $^

debug_test : test_main.d.o image.d.o fractal.d.o
	$(LD) -o $@ $^

%.d.o : %.c
	$(CC) -c $(CCOPT_D) -o $@ $<

%.r.o : %.c
	$(CC) -c $(CCOPT_R) -o $@ $<

clean :
	- rm *.o
	- rm *.hdrdep.bak

cleanall : clean
	- rm release debug debug_test
	- echo > makefile.hdrdep

mrproper : cleanall

# Advanced feature: #include file dependencies
# Run `make depends` before `make all`

depends :
	makedepend -f makefile.hdrdep *.c

include makefile.hdrdep
