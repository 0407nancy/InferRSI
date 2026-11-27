def histogram(values):
    """Count integer occurrences without mutating the input."""
    return {value: values.count(value) for value in set(values)}
