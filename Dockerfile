FROM quay.io/bcdev/xcube-python-deps:0.4.2

LABEL maintainer="helge.dzierzon@brockmann-consult.de"
LABEL name=xcube-cci

RUN echo "Building docker using args:"

USER xcube

WORKDIR /home/xcube

ADD --chown=1000:1000 environment.yml environment.yml
RUN conda env update -n xcube

ADD --chown=1000:1000 ./ .
RUN source activate xcube && pip install .

CMD ["/bin/bash", "-c", "source activate xcube && xcube cci info"]