def is_prime(n):
    if n < 2:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False

    for i in range(3, int(n ** 0.5) + 1, 2):
        if n % i == 0:
            return False
    return True


def generate_primes(limit):
    return [num for num in range(2, limit + 1) if is_prime(num)]


if __name__ == "__main__":
    limit = int(input("Enter a number to generate primes up to: "))
    primes = generate_primes(limit)
    print(f"Prime numbers up to {limit}: {primes}")
