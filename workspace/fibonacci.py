import sys

def generate_fibonacci(n):
    fib_sequence = [0, 1]
    while len(fib_sequence) < n:
        next_value = fib_sequence[-1] + fib_sequence[-2]
        fib_sequence.append(next_value)
    return fib_sequence[:n]

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python fibonacci.py <number_of_terms>")
        sys.exit(1)
    
    try:
        n = int(sys.argv[1])
        if n <= 0:
            print("Please enter a positive integer greater than 0.")
            sys.exit(1)
        elif n > 100:
            print("Please enter a number less than or equal to 100.")
            sys.exit(1)
    except ValueError:
        print("Invalid input. Please enter a positive integer.")
        sys.exit(1)
    
    fib_sequence = generate_fibonacci(n)
    print(f"First {n} Fibonacci numbers:")
    print(fib_sequence)
