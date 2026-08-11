"""Generators for the synthetic catalog and the question stream.

Nothing here is imported by the service. The generators produce the substrate;
the service reads it. Keeping them apart is deliberate — a context layer that
could see its own generator would be grading its own homework.
"""
