FROM quay.io/bcdev/xcube-python-deps:0.4.2

ARG XCUBE_USER_NAME=xcube
ARG XCUBE_GEN_VERSION=latest

LABEL maintainer="helge.dzierzon@brockmann-consult.de"
LABEL name=xcube-cci
LABEL xcube_user=${XCUBE_USER_NAME}
LABEL xcube_gen_version=${XCUBE_GEN_VERSION}

USER xcube

RUN mkdir /home/xcube/xcube-cci
WORKDIR /home/xcube/xcube-cci

ADD --chown=1000:1000 environment.yml environment.yml
RUN conda env update -n xcube

ADD --chown=1000:1000 ./ .
RUN source activate xcube && pip install .

CMD ["/bin/bash", "-c", "source activate xcube && xcube cci info"]