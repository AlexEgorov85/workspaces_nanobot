import sys

def fibonacci(n):
    \"\"\"Generate the first n Fibonacci numbers.\"\"\"
    if n <= 0:
        return []
    elif n == 1:
        return [0]
    elif n == 2:
        return [0, 1]
    
    fib_sequence = [0, 1]
    for i in range(2, n):
        next_fib = fib_sequence[-1] + fib_sequence[-2]
        fib_sequence.append(next_fib)
    return fib_sequence

def main():
    if len(sys.argv) != 2:
        print(\"Usage: python fibonacci.py <N>\")
        sys.exit(1)
    
    try:
        n = int(sys.argv[1])
        if n < 0:
            print(\"Please provide a non-negative integer.\")
            sys.exit(1)
    except ValueError:
        print(\"Please provide a valid integer.\")
        sys.exit(1)
    
    fib_numbers = fibonacci(n)
    print(fib_numbers)

if __name__ == \"__main__\":
    main()