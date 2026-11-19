"""Helper functions of the example model."""


def stress_pd(pd, multiplier, cap=1.0):
    """Probability of default under stress, capped at 100%."""
    return min(pd * multiplier, cap)


def expected_loss(exposure, pd, lgd):
    """Expected loss = exposure x probability of default x loss given default."""
    return exposure * pd * lgd
